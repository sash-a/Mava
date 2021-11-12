# python3
# Copyright 2021 InstaDeep Ltd. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MADQN system trainer implementation."""

import copy
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

import numpy as np
import reverb
import sonnet as snt
from sonnet.src.base import NO_VARIABLES_ERROR
import tensorflow as tf
from tensorflow.python.ops.gen_math_ops import NotEqual
from tensorflow.python.ops.variables import trainable_variables
import tree
import trfl
from acme.tf import utils as tf2_utils
from acme.types import NestedArray
from acme.utils import counting, loggers

import mava
from mava import types as mava_types
from mava.adders import reverb as reverb_adders
from mava.components.tf.modules.communication import BaseCommunicationModule
from mava.components.tf.networks.monotonic import MonotonicMixingNetwork
from mava.systems.tf import savers as tf2_savers
from mava.utils import training_utils as train_utils
from mava.utils.sort_utils import sort_str_num
from trfl.indexing_ops import batched_index

train_utils.set_growing_gpu_memory()


class MADQNTrainer(mava.Trainer):
    """MADQN trainer.
    This is the trainer component of a MADQN system. IE it takes a dataset as input
    and implements update functionality to learn from this dataset.
    """

    def __init__(
        self,
        agents: List[str],
        agent_types: List[str],
        q_networks: Dict[str, snt.Module],
        target_q_networks: Dict[str, snt.Module],
        target_update_period: int,
        dataset: tf.data.Dataset,
        optimizer: Union[Dict[str, snt.Optimizer], snt.Optimizer],
        discount: float,
        agent_net_keys: Dict[str, str],
        checkpoint_minute_interval: int,
        max_gradient_norm: float = None,
        importance_sampling_exponent: Optional[float] = None,
        replay_client: Optional[reverb.TFClient] = None,
        max_priority_weight: float = 0.9,
        fingerprint: bool = False,
        counter: counting.Counter = None,
        logger: loggers.Logger = None,
        checkpoint: bool = True,
        checkpoint_subpath: str = "~/mava/",
        replay_table_name: str = reverb_adders.DEFAULT_PRIORITY_TABLE,
        communication_module: Optional[BaseCommunicationModule] = None,
        learning_rate_scheduler_fn: Optional[Callable[[int], None]] = None,
    ):
        """Initialise MADQN trainer

        Args:
            agents (List[str]): agent ids, e.g. "agent_0".
            agent_types (List[str]): agent types, e.g. "speaker" or "listener".
            q_networks (Dict[str, snt.Module]): q-value networks.
            target_q_networks (Dict[str, snt.Module]): target q-value networks.
            target_update_period (int): number of steps before updating target networks.
            dataset (tf.data.Dataset): training dataset.
            optimizer (Union[snt.Optimizer, Dict[str, snt.Optimizer]]): type of
                optimizer for updating the parameters of the networks.
            discount (float): discount factor for TD updates.
            agent_net_keys: (dict, optional): specifies what network each agent uses.
                Defaults to {}.
            checkpoint_minute_interval (int): The number of minutes to wait between
                checkpoints.
            max_gradient_norm (float, optional): maximum allowed norm for gradients
                before clipping is applied. Defaults to None.
            fingerprint (bool, optional): whether to apply replay stabilisation using
                policy fingerprints. Defaults to False.
            counter (counting.Counter, optional): step counter object. Defaults to None.
            logger (loggers.Logger, optional): logger object for logging trainer
                statistics. Defaults to None.
            checkpoint (bool, optional): whether to checkpoint networks. Defaults to
                True.
            checkpoint_subpath (str, optional): subdirectory for storing checkpoints.
                Defaults to "~/mava/".
            communication_module (BaseCommunicationModule): module for communication
                between agents. Defaults to None.
            learning_rate_scheduler_fn: function/class that takes in a trainer step t
                and returns the current learning rate.
        """

        self._agents = agents
        self._agent_types = agent_types
        self._agent_net_keys = agent_net_keys
        self._checkpoint = checkpoint
        self._learning_rate_scheduler_fn = learning_rate_scheduler_fn

        # Store online and target q-networks.
        self._q_networks = q_networks
        self._target_q_networks = target_q_networks

        # General learner book-keeping and loggers.
        self._counter = counter or counting.Counter()
        self._logger = logger

        # Other learner parameters.
        self._discount = discount
        # Set up gradient clipping.
        if max_gradient_norm is not None:
            self._max_gradient_norm = tf.convert_to_tensor(max_gradient_norm)
        else:  # A very large number. Infinity results in NaNs.
            self._max_gradient_norm = tf.convert_to_tensor(1e10)

        self._fingerprint = fingerprint

        # Necessary to track when to update target networks.
        self._num_steps = tf.Variable(0, trainable=False)
        self._target_update_period = target_update_period

        # Create an iterator to go through the dataset.
        self._iterator = dataset

        # Importance sampling hyper-parameters
        self._max_priority_weight = max_priority_weight
        self._importance_sampling_exponent = importance_sampling_exponent

        # Replay client for updating priorities.
        self._replay_client = replay_client
        self._replay_table_name = replay_table_name

        # NOTE We make replay_client optional to make changes to MADQN trainer
        # compatible with the other systems that inherit from it (VDN, QMIX etc.)
        # TODO Include importance sampling in the other systems so that we can remove
        # this check.
        if self._importance_sampling_exponent is not None:
            assert isinstance(self._replay_client, reverb.Client)

        # Dictionary with network keys for each agent.
        self.unique_net_keys = sort_str_num(self._q_networks.keys())

        # Create optimizers for different agent types.
        if not isinstance(optimizer, dict):
            self._optimizers: Dict[str, snt.Optimizer] = {}
            for agent in self.unique_net_keys:
                self._optimizers[agent] = copy.deepcopy(optimizer)
        else:
            self._optimizers = optimizer

        # Expose the variables.
        q_networks_to_expose = {}
        self._system_network_variables: Dict[str, Dict[str, snt.Module]] = {
            "q_network": {},
        }
        for agent_key in self.unique_net_keys:
            q_network_to_expose = self._target_q_networks[agent_key]

            q_networks_to_expose[agent_key] = q_network_to_expose

            self._system_network_variables["q_network"][
                agent_key
            ] = q_network_to_expose.variables

        # Checkpointer
        self._system_checkpointer = {}
        if checkpoint:
            for agent_key in self.unique_net_keys:

                checkpointer = tf2_savers.Checkpointer(
                    directory=checkpoint_subpath,
                    time_delta_minutes=checkpoint_minute_interval,
                    objects_to_save={
                        "counter": self._counter,
                        "q_network": self._q_networks[agent_key],
                        "target_q_network": self._target_q_networks[agent_key],
                        "optimizer": self._optimizers,
                        "num_steps": self._num_steps,
                    },
                    enable_checkpointing=checkpoint,
                )

                self._system_checkpointer[agent_key] = checkpointer

        # Do not record timestamps until after the first learning step is done.
        # This is to avoid including the time it takes for actors to come online and
        # fill the replay buffer.

        self._timestamp: Optional[float] = None

    def get_trainer_steps(self) -> float:
        """get trainer step count

        Returns:
            float: number of trainer steps
        """

        return self._num_steps.numpy()

    def _update_target_networks(self) -> None:
        """Sync the target network parameters with the latest online network
        parameters"""

        for key in self.unique_net_keys:
            # Update target network.
            online_variables = (*self._q_networks[key].variables,)

            target_variables = (*self._target_q_networks[key].variables,)

            # Make online -> target network update ops.
            if tf.math.mod(self._num_steps, self._target_update_period) == 0:
                for src, dest in zip(online_variables, target_variables):
                    dest.assign(src)

        # if self._mixing_network and isinstance(
        #     self._mixing_network, MonotonicMixingNetwork
        # ):
        #     # NOTE These shouldn't really be in the agent for loop.
        #     online_variables = [*self._mixing_network.variables]
        #     target_variables = [*self._target_mixing_network.variables]

        #     # Make online -> target network update ops.
        #     for src, dest in zip(online_variables, target_variables):
        #         dest.assign(src)
        self._num_steps.assign_add(1)

    def _update_sample_priorities(self, keys: tf.Tensor, priorities: tf.Tensor) -> None:
        """Update sample priorities in replay table using importance weights.

        Args:
            keys (tf.Tensor): Keys of the replay samples.
            priorities (tf.Tensor): New priorities for replay samples.
        """
        # Maybe update the sample priorities in the replay buffer.
        if (
            self._importance_sampling_exponent is not None
            and self._replay_client is not None
        ):
            self._replay_client.mutate_priorities(
                table=self._replay_table_name,
                updates=dict(zip(keys.numpy(), priorities.numpy())),
            )

    def _get_feed(
        self,
        o_tm1_trans: Dict[str, np.ndarray],
        o_t_trans: Dict[str, np.ndarray],
        a_tm1: Dict[str, np.ndarray],
        agent: str,
    ) -> Tuple[tf.Tensor, tf.Tensor, tf.Tensor]:
        """get data to feed to the agent networks

        Args:
            o_tm1_trans (Dict[str, np.ndarray]): transformed (e.g. using observation
                network) observation at timestep t-1
            o_t_trans (Dict[str, np.ndarray]): transformed observation at timestep t
            a_tm1 (Dict[str, np.ndarray]): action at timestep t-1
            agent (str): agent id

        Returns:
            Tuple[tf.Tensor, tf.Tensor, tf.Tensor]: agent network feeds, observations
                at t-1, t and action at time t.
        """

        # Decentralised
        o_tm1_feed = o_tm1_trans[agent].observation
        o_t_feed = o_t_trans[agent].observation
        a_tm1_feed = a_tm1[agent]

        return o_tm1_feed, o_t_feed, a_tm1_feed

    def step(self) -> None:
        """trainer step to update the parameters of the agents in the system"""

        # Run the learning step.
        fetches = self._step()

        # Compute elapsed time.
        timestamp = time.time()
        if self._timestamp:
            elapsed_time = timestamp - self._timestamp
        else:
            elapsed_time = 0
        self._timestamp = timestamp  # type: ignore

        # Update our counts and record it.
        counts = self._counter.increment(steps=1, walltime=elapsed_time)
        fetches.update(counts)

        # Checkpoint and attempt to write the logs.
        if self._checkpoint:
            train_utils.checkpoint_networks(self._system_checkpointer)

        if self._logger:
            self._logger.write(fetches)

    # @tf.function
    def _forward_backward(self) -> Tuple:
        # Get data from replay (dropping extras if any). Note there is no
        # extra data here because we do not insert any into Reverb.
        inputs = next(self._iterator)

        self._forward(inputs)

        self._backward()

        extras = {}

        if self._importance_sampling_exponent is not None:
            extras.update(
                {"keys": self._sample_keys, "priorities": self._sample_priorities}
            )

        # Return Q-value losses.
        fetches = self._q_network_losses

        return fetches, extras

    # @tf.function
    def _step(self) -> Dict:
        """Trainer forward and backward passes."""

        # Update the target networks
        self._update_target_networks()

        fetches, extras = self._forward_backward()

        # Maybe update priorities.
        # NOTE _update_sample_priorities must happen outside of
        # tf.function. That is why we seperate out forward_backward().
        if self._importance_sampling_exponent is not None:
            self._update_sample_priorities(extras["keys"], extras["priorities"])

        # Log losses
        return fetches

    def _forward(self, inputs: reverb.ReplaySample) -> None:
        """Trainer forward pass

        Args:
            inputs (Any): input data from the data table (transitions)
        """

        # Get info about the samples from reverb.
        sample_info = inputs.info
        sample_keys = tf.transpose(inputs.info.key)
        sample_probs = tf.transpose(sample_info.probability)

        # Initialize sample priorities at zero.
        sample_priorities = np.zeros(len(inputs.info.key))

        # Unpack input data as follows:
        # o_tm1 = dictionary of observations one for each agent
        # a_tm1 = dictionary of actions taken from obs in o_tm1
        # r_t = dictionary of rewards or rewards sequences
        #   (if using N step transitions) ensuing from actions a_tm1
        # d_t = environment discount ensuing from actions a_tm1.
        #   This discount is applied to future rewards after r_t.
        # o_t = dictionary of next observations or next observation sequences
        # e_t [Optional] = extra data that the agents persist in replay.
        trans = mava_types.Transition(*inputs.data)

        o_tm1, o_t, a_tm1, r_t, d_t, e_tm1, e_t = (
            trans.observations,
            trans.next_observations,
            trans.actions,
            trans.rewards,
            trans.discounts,
            trans.extras,
            trans.next_extras,
        )

        with tf.GradientTape(persistent=True) as tape:
            q_network_losses: Dict[str, NestedArray] = {}

            for agent in self._agents:
                agent_key = self._agent_net_keys[agent]

                # Cast the additional discount to match the environment discount dtype.
                discount = tf.cast(self._discount, dtype=d_t[agent].dtype)

                # Maybe transform the observation before feeding into policy and critic.
                # Transforming the observations this way at the start of the learning
                # step effectively means that the policy and critic share observation
                # network weights.

                o_tm1_feed, o_t_feed, a_tm1_feed = self._get_feed(
                    o_tm1, o_t, a_tm1, agent
                )

                if self._fingerprint:
                    f_tm1 = e_tm1["fingerprint"]
                    f_tm1 = tf.convert_to_tensor(f_tm1)
                    f_tm1 = tf.cast(f_tm1, "float32")

                    f_t = e_t["fingerprint"]
                    f_t = tf.convert_to_tensor(f_t)
                    f_t = tf.cast(f_t, "float32")

                    q_tm1 = self._q_networks[agent_key](o_tm1_feed, f_tm1)
                    q_t_value = self._target_q_networks[agent_key](o_t_feed, f_t)
                    q_t_selector = self._q_networks[agent_key](o_t_feed, f_t)
                else:
                    q_tm1 = self._q_networks[agent_key](o_tm1_feed)
                    q_t_value = self._target_q_networks[agent_key](o_t_feed)
                    q_t_selector = self._q_networks[agent_key](o_t_feed)

                # Q-network learning
                loss, loss_extras = trfl.double_qlearning(
                    q_tm1,
                    a_tm1_feed,
                    r_t[agent],
                    discount * d_t[agent],
                    q_t_value,
                    q_t_selector,
                )

                # Maybe do importance sampling.
                if self._importance_sampling_exponent is not None:
                    importance_weights = 1.0 / sample_probs  # [B]
                    importance_weights **= self._importance_sampling_exponent
                    importance_weights /= tf.reduce_max(importance_weights)

                    # Reweight loss.
                    loss *= tf.cast(importance_weights, loss.dtype)  # [B]

                    # Update priorities.
                    errors = loss_extras.td_error
                    abs_errors = tf.abs(errors)
                    mean_priority = tf.reduce_mean(abs_errors, axis=0)
                    max_priority = tf.reduce_max(abs_errors, axis=0)
                    sample_priorities += (
                        self._max_priority_weight * max_priority
                        + (1 - self._max_priority_weight) * mean_priority
                    )

                loss = tf.reduce_mean(loss)
                q_network_losses[agent] = {"policy_loss": loss}

        # Store losses and tape
        self._q_network_losses = q_network_losses
        self.tape = tape

        # Store sample keys and priorities
        self._sample_keys = sample_keys
        self._sample_priorities = sample_priorities / len(
            self._agents
        )  # averaged over agents.

    def _backward(self) -> None:
        """Trainer backward pass updating network parameters"""

        q_network_losses = self._q_network_losses
        tape = self.tape
        for agent in self._agents:
            agent_key = self._agent_net_keys[agent]

            # Get trainable variables
            q_network_variables = self._q_networks[agent_key].trainable_variables

            # Compute gradients
            gradients = tape.gradient(q_network_losses[agent], q_network_variables)

            # Clip gradients.
            gradients = tf.clip_by_global_norm(gradients, self._max_gradient_norm)[0]

            # Apply gradients.
            self._optimizers[agent_key].apply(gradients, q_network_variables)

        train_utils.safe_del(self, "tape")

    def get_variables(self, names: Sequence[str]) -> Dict[str, Dict[str, np.ndarray]]:
        """get network variables

        Args:
            names (Sequence[str]): network names

        Returns:
            Dict[str, Dict[str, np.ndarray]]: network variables
        """

        variables: Dict[str, Dict[str, np.ndarray]] = {}
        for network_type in names:
            variables[network_type] = {
                agent: tf2_utils.to_numpy(
                    self._system_network_variables[network_type][agent]
                )
                for agent in self.unique_net_keys
            }
        return variables

    def after_trainer_step(self) -> None:
        """Optionally decay lr after every training step."""
        if self._learning_rate_scheduler_fn:
            self._decay_lr(self._num_steps)
            info: Dict[str, Dict[str, float]] = {}
            for agent in self._agents:
                info[agent] = {}
                info[agent]["learning_rate"] = self._optimizers[
                    self._agent_net_keys[agent]
                ].learning_rate
            if self._logger:
                self._logger.write(info)

    def _decay_lr(self, trainer_step: int) -> None:
        """Decay lr.

        Args:
            trainer_step : trainer step time t.
        """
        train_utils.decay_lr(
            self._learning_rate_scheduler_fn, self._optimizers, trainer_step
        )


class MADQNRecurrentTrainer(MADQNTrainer):
    """Recurrent MADQN trainer.
    This is the trainer component of a MADQN system. IE it takes a dataset as input
    and implements update functionality to learn from this dataset.
    """

    def __init__(
        self,
        agents: List[str],
        agent_types: List[str],
        q_networks: Dict[str, snt.Module],
        target_q_networks: Dict[str, snt.Module],
        target_update_period: int,
        dataset: tf.data.Dataset,
        optimizer: Union[snt.Optimizer, Dict[str, snt.Optimizer]],
        discount: float,
        agent_net_keys: Dict[str, str],
        checkpoint_minute_interval: int,
        max_gradient_norm: float = None,
        counter: counting.Counter = None,
        logger: loggers.Logger = None,
        checkpoint: bool = True,
        checkpoint_subpath: str = "~/mava/",
        learning_rate_scheduler_fn: Optional[Callable[[int], None]] = None,
        mixing_network=None,
        target_mixing_network=None,
    ):
        """Initialise recurrent MADQN trainer

        Args:
            agents (List[str]): agent ids, e.g. "agent_0".
            agent_types (List[str]): agent types, e.g. "speaker" or "listener".
            q_networks (Dict[str, snt.Module]): q-value networks.
            target_q_networks (Dict[str, snt.Module]): target q-value networks.
            target_update_period (int): number of steps before updating target networks.
            dataset (tf.data.Dataset): training dataset.
            optimizer (Union[snt.Optimizer, Dict[str, snt.Optimizer]]): type of
                optimizer for updating the parameters of the networks.
            discount (float): discount factor for TD updates.
            agent_net_keys: (dict, optional): specifies what network each agent uses.
                Defaults to {}.
            checkpoint_minute_interval (int): The number of minutes to wait between
                checkpoints.
            max_gradient_norm (float, optional): maximum allowed norm for gradients
                before clipping is applied. Defaults to None.
            counter (counting.Counter, optional): step counter object. Defaults to None.
            logger (loggers.Logger, optional): logger object for logging trainer
                statistics. Defaults to None.
            fingerprint (bool, optional): whether to apply replay stabilisation using
                policy fingerprints. Defaults to False.
            checkpoint (bool, optional): whether to checkpoint networks. Defaults to
                True.
            checkpoint_subpath (str, optional): subdirectory for storing checkpoints.
                Defaults to "~/mava/".
            communication_module (BaseCommunicationModule): module for communication
                between agents. Defaults to None.
            learning_rate_scheduler_fn: function/class that takes in a trainer step t
                and returns the current learning rate.
        """

        self._agents = agents
        self._agent_types = agent_types
        self._agent_net_keys = agent_net_keys
        self._checkpoint = checkpoint
        self._learning_rate_scheduler_fn = learning_rate_scheduler_fn
        self._double_q_learning = False

        # Store online and target q-networks.
        self._q_networks = q_networks
        self._target_q_networks = target_q_networks

        # General learner book-keeping and loggers.
        self._counter = counter or counting.Counter()
        self._logger = logger

        # Other learner parameters.
        self._discount = discount
        # Set up gradient clipping.
        if max_gradient_norm is not None:
            self._max_gradient_norm = tf.convert_to_tensor(max_gradient_norm)
        else:  # A very large number. Infinity results in NaNs.
            self._max_gradient_norm = tf.convert_to_tensor(1e10)

        # Necessary to track when to update target networks.
        self._num_steps = tf.Variable(0, trainable=False)
        self._target_update_period = target_update_period

        # Create an iterator to go through the dataset.
        self._iterator = dataset

        # Dictionary with network keys for each agent.
        self.unique_net_keys = sort_str_num(self._q_networks.keys())

        # Create optimizers for different agent types.
        if not isinstance(optimizer, dict):
            self._optimizers: Dict[str, snt.Optimizer] = {}
            for agent in self.unique_net_keys:
                self._optimizers[agent] = copy.deepcopy(optimizer)
        else:
            self._optimizers = optimizer

        # Expose the variables.
        q_networks_to_expose = {}
        self._system_network_variables: Dict[str, Dict[str, snt.Module]] = {
            "q_network": {},
        }
        for agent_key in self.unique_net_keys:
            q_network_to_expose = self._target_q_networks[agent_key]

            q_networks_to_expose[agent_key] = q_network_to_expose

            self._system_network_variables["q_network"][
                agent_key
            ] = q_network_to_expose.variables

        # Checkpointer
        self._system_checkpointer = {}
        if checkpoint:
            for agent_key in self.unique_net_keys:

                checkpointer = tf2_savers.Checkpointer(
                    directory=checkpoint_subpath,
                    time_delta_minutes=checkpoint_minute_interval,
                    objects_to_save={
                        "counter": self._counter,
                        "q_network": self._q_networks[agent_key],
                        "target_q_network": self._target_q_networks[agent_key],
                        "optimizer": self._optimizers,
                        "num_steps": self._num_steps,
                    },
                    enable_checkpointing=checkpoint,
                )

                self._system_checkpointer[agent_key] = checkpointer

        # Do not record timestamps until after the first learning step is done.
        # This is to avoid including the time it takes for actors to come online and
        # fill the replay buffer.

        self._timestamp: Optional[float] = None

        self._mixing_network = mixing_network
        self._target_mixing_network = target_mixing_network

        self._include_agent_id = True
        self._include_prev_action = True

    def _update_target_networks(self) -> None:
        """Sync the target network parameters with the latest online network
        parameters"""

        for key in self.unique_net_keys:
            # Update target network.
            online_variables = (*self._q_networks[key].variables,)

            target_variables = (*self._target_q_networks[key].variables,)

            # Make online -> target network update ops.
            if tf.math.mod(self._num_steps, self._target_update_period) == 0:
                for src, dest in zip(online_variables, target_variables):
                    dest.assign(src)

        if self._mixing_network and isinstance(
            self._mixing_network,
            mava.components.tf.modules.mixing.monotonic.MonotonicMixingNetwork,
        ):
            # NOTE These shouldn't really be in the agent for loop.
            online_variables = [*self._mixing_network.variables]
            target_variables = [*self._target_mixing_network.variables]

            # Make online -> target network update ops.
            for src, dest in zip(online_variables, target_variables):
                dest.assign(src)
        self._num_steps.assign_add(1)

    @tf.function
    def _step(
        self,
    ) -> Dict[str, Dict[str, Any]]:
        """Trainer forward and backward passes."""

        # Update the target networks
        self._update_target_networks()

        # Get data from replay (dropping extras if any). Note there is no
        # extra data here because we do not insert any into Reverb.
        inputs = next(self._iterator)

        self._forward(inputs)

        self._backward()

        # Log losses per agent
        return {agent: {"policy_loss": self.loss} for agent in self._agents}

    def add_info(self, observations, actions):
        # action [T,B,1] or [T,B,1]
        for i, agent in enumerate(self._agents):
            env_observtion = observations[agent].observation  # [B,T,OBS_SIZE]
            one_hot_agent_id = np.zeros(len(self._agents))
            one_hot_agent_id[i] = 1
            one_hot_agent_id = tf.convert_to_tensor(
                one_hot_agent_id, dtype=env_observtion.dtype
            )
            broadcast_shape = list(env_observtion.shape[0:-1]) + [len(self._agents)]
            one_hot_agent_id = tf.broadcast_to(one_hot_agent_id, broadcast_shape)

            prev_actions = actions[agent][:-1]
            one_hot_zero_action_shape = [1] + prev_actions.shape[1]
            one_hot_zero_action = tf.zeros(observations[agent].legal_actions)
            prev_actions = tf.zeros + prev_actions

            env_observtion = tf.concat([env_observtion, one_hot_agent_id], axis=-1)

            observations[agent] = mava_types.OLT(
                observation=env_observtion,
                legal_actions=observations[agent].legal_actions,
                terminal=observations[agent].terminal,
            )

        # for agent,observation in observations.items():
        return observations, actions

    def _forward(self, inputs: Any) -> None:
        """Trainer forward pass

        Args:
            inputs (Any): input data from the data table (transitions)
        """

        data = tree.map_structure(
            lambda v: tf.expand_dims(v, axis=0) if len(v.shape) <= 1 else v, inputs.data
        )
        data = tf2_utils.batch_to_sequence(data)

        observations, actions, rewards, discounts, _, _ = (
            data.observations,
            data.actions,
            data.rewards,
            data.discounts,
            data.start_of_episode,
            data.extras,
        )

        # tf.print(data.extras)
        # tf.print()
        # tf.print(tf.shape(data.extras["s_t"]))  # [61 32 48] [T, B, S_DIM]
        global_env_state = data.extras["s_t"]
        # Add agent id
        # observations, actions = self.add_info(observations, actions)
        # Using extra directly from inputs due to shape.
        core_state = tree.map_structure(
            lambda s: s[:, 0, :], inputs.data.extras["core_states"]
        )

        zero_padding_mask = inputs.data.extras["filled"]  # [B, T]
        zero_padding_mask = tf.transpose(zero_padding_mask, [1, 0])
        zero_padding_mask = tf.expand_dims(zero_padding_mask, axis=-1)

        with tf.GradientTape(persistent=True) as tape:

            q_values_chosen_actions_all_agents = []
            target_q_max_all_agents = []
            reward_all_agents = []
            env_discounts_all_agents = []

            for agent in self._agents:
                agent_key = self._agent_net_keys[agent]

                q_values, _ = snt.static_unroll(
                    self._q_networks[agent_key],
                    observations[agent].observation,
                    core_state[agent][0],
                )

                q_values_chosen_actions = batched_index(q_values, actions[agent])
                q_values_chosen_actions_all_agents.append(q_values_chosen_actions)

                target_q_values, _ = snt.static_unroll(
                    self._target_q_networks[agent_key],
                    observations[agent].observation,
                    core_state[agent][0],
                )

                agent_legal_actions = tf.cast(observations[agent].legal_actions, "bool")

                target_q_values = tf.where(
                    agent_legal_actions,
                    target_q_values,
                    -9999999
                    # -np.inf
                )

                if self._double_q_learning:
                    target_q_selector = tf.where(
                        agent_legal_actions, q_values, -99999999
                    )
                    selected_actions = tf.argmax(target_q_selector, axis=-1)
                    target_q_max = trfl.batched_index(target_q_values, selected_actions)
                else:
                    target_q_max = tf.reduce_max(target_q_values, axis=-1)
                target_q_max_all_agents.append(target_q_max)
                reward_all_agents.append(rewards[agent])
                env_discounts_all_agents.append(discounts[agent])

            q_values_chosen_actions_all_agents = tf.stack(
                q_values_chosen_actions_all_agents, axis=-1
            )
            target_q_max_all_agents = tf.stack(target_q_max_all_agents, axis=-1)
            reward_all_agents = tf.stack(reward_all_agents, axis=-1)  # [T,B,A]
            env_discounts_all_agents = tf.stack(env_discounts_all_agents, axis=-1)

            # Mixing in vdn/qmix
            if self._mixing_network and self._target_mixing_network:
                q_values_chosen_actions_all_agents = self._mixing_network(
                    q_values_chosen_actions_all_agents,
                    global_env_state=global_env_state,
                )
                target_q_max_all_agents = self._target_mixing_network(
                    target_q_max_all_agents, global_env_state=global_env_state
                )
                reward_all_agents = tf.reduce_mean(
                    reward_all_agents, axis=-1, keepdims=True
                )
                # TODO Assumes all agents have the same env discount
                env_discounts_all_agents = tf.reduce_mean(
                    env_discounts_all_agents, axis=-1, keepdims=True
                )

            # Calc targets
            trainer_discount = tf.cast(self._discount, env_discounts_all_agents.dtype)
            pcont = trainer_discount * env_discounts_all_agents
            # tf.print(
            #     reward_all_agents.shape,
            #     pcont.shape,
            #     target_q_max_all_agents.shape,
            #     q_values_chosen_actions_all_agents.shape,
            #     zero_padding_mask.shape,
            # )
            # raise NotADirectoryError
            targets = reward_all_agents[:-1] + pcont[:-1] * target_q_max_all_agents[1:]

            # Td-error
            td_error = q_values_chosen_actions_all_agents[:-1] - tf.stop_gradient(
                targets
            )

            zero_padding_mask = tf.cast(zero_padding_mask, td_error.dtype)

            # tf.print(tf.shape(zero_padding_mask_all_agents))
            # tf.print(tf.shape(td_error))
            # raise NotADirectoryError

            masked_td_error = td_error * zero_padding_mask[:-1]

            loss = tf.reduce_sum(masked_td_error ** 2) / tf.reduce_sum(
                zero_padding_mask
            )

            # tf.print()

            # Loss is MSE scaled by 0.5, so the gradient is equal to the TD error.
            # self.loss = 0.5 *
            self.loss = loss
            self.tape = tape

    def _backward(self) -> None:
        """Trainer backward pass updating network parameters"""

        # Calculate the gradients and update the networks
        # for agent in self._agents:
        #     agent_key = self._agent_net_keys[agent]
        #     # Get trainable variables.
        #     trainable_variables = self._q_networks[agent_key].trainable_variables

        #     # Compute gradients.
        #     gradients = self.tape.gradient(self.loss, trainable_variables)

        #     # Clip gradients.
        #     gradients = tf.clip_by_global_norm(gradients, self._max_gradient_norm)[0]

        #     # Apply gradients.
        #     self._optimizers[agent_key].apply(gradients, trainable_variables)

        trainable_variables = list(self._q_networks.values())[0].trainable_variables
        # Only for qmix
        # Update mixing network
        if self._mixing_network and isinstance(
            self._mixing_network,
            mava.components.tf.modules.mixing.monotonic.MonotonicMixingNetwork,
        ):
            trainable_variables = list(trainable_variables) + list(
                self._mixing_network.trainable_variables
            )
            # gradients = self.tape.gradient(self.loss, variables)

        gradients = self.tape.gradient(self.loss, trainable_variables)
        gradients = tf.clip_by_global_norm(gradients, self._max_gradient_norm)[0]
        list(self._optimizers.values())[0].apply(gradients, trainable_variables)
        # Delete the tape manually because of the persistent=True flag.
        train_utils.safe_del(self, "tape")


class MADQNRecurrentCommTrainer(MADQNTrainer):
    """Recurrent MADQN trainer with communication.
    This is the trainer component of a MADQN system. IE it takes a dataset as input
    and implements update functionality to learn from this dataset.
    """

    def __init__(
        self,
        agents: List[str],
        agent_types: List[str],
        q_networks: Dict[str, snt.Module],
        target_q_networks: Dict[str, snt.Module],
        target_update_period: int,
        dataset: tf.data.Dataset,
        optimizer: Union[snt.Optimizer, Dict[str, snt.Optimizer]],
        discount: float,
        agent_net_keys: Dict[str, str],
        checkpoint_minute_interval: int,
        communication_module: BaseCommunicationModule,
        max_gradient_norm: float = None,
        fingerprint: bool = False,
        counter: counting.Counter = None,
        logger: loggers.Logger = None,
        checkpoint: bool = True,
        checkpoint_subpath: str = "~/mava/",
        learning_rate_scheduler_fn: Optional[Callable[[int], None]] = None,
    ):
        """Initialise recurrent MADQN trainer with communication

        Args:
            agents (List[str]): agent ids, e.g. "agent_0".
            agent_types (List[str]): agent types, e.g. "speaker" or "listener".
            q_networks (Dict[str, snt.Module]): q-value networks.
            target_q_networks (Dict[str, snt.Module]): target q-value networks.
            target_update_period (int): number of steps before updating target networks.
            dataset (tf.data.Dataset): training dataset.
            optimizer (Union[snt.Optimizer, Dict[str, snt.Optimizer]]): type of
                optimizer for updating the parameters of the networks.
            discount (float): discount factor for TD updates.
            agent_net_keys: (dict, optional): specifies what network each agent uses.
                Defaults to {}.
            checkpoint_minute_interval (int): The number of minutes to wait between
                checkpoints.
            communication_module (BaseCommunicationModule): module for communication
                between agents.
            max_gradient_norm (float, optional): maximum allowed norm for gradients
                before clipping is applied. Defaults to None.
            fingerprint (bool, optional): whether to apply replay stabilisation using
                policy fingerprints. Defaults to False.
            counter (counting.Counter, optional): step counter object. Defaults to None.
            logger (loggers.Logger, optional): logger object for logging trainer
                statistics. Defaults to None.
            checkpoint (bool, optional): whether to checkpoint networks. Defaults to
                True.
            checkpoint_subpath (str, optional): subdirectory for storing checkpoints.
                Defaults to "~/mava/".
            learning_rate_scheduler_fn: function/class that takes in a trainer step t
                and returns the current learning rate.
        """

        super().__init__(
            agents=agents,
            agent_types=agent_types,
            q_networks=q_networks,
            target_q_networks=target_q_networks,
            target_update_period=target_update_period,
            dataset=dataset,
            optimizer=optimizer,
            discount=discount,
            agent_net_keys=agent_net_keys,
            checkpoint_minute_interval=checkpoint_minute_interval,
            max_gradient_norm=max_gradient_norm,
            fingerprint=fingerprint,
            counter=counter,
            logger=logger,
            checkpoint=checkpoint,
            checkpoint_subpath=checkpoint_subpath,
            learning_rate_scheduler_fn=learning_rate_scheduler_fn,
        )

        self._communication_module = communication_module

    def _forward(self, inputs: Any) -> None:
        """Trainer forward pass

        Args:
            inputs (Any): input data from the data table (transitions)
        """

        data = tree.map_structure(
            lambda v: tf.expand_dims(v, axis=0) if len(v.shape) <= 1 else v, inputs.data
        )
        data = tf2_utils.batch_to_sequence(data)

        observations, actions, rewards, discounts, _, _ = (
            data.observations,
            data.actions,
            data.rewards,
            data.discounts,
            data.start_of_episode,
            data.extras,
        )

        # Using extra directly from inputs due to shape.
        core_state = tree.map_structure(
            lambda s: s[:, 0, :], inputs.data.extras["core_states"]
        )
        core_message = tree.map_structure(
            lambda s: s[:, 0, :], inputs.data.extras["core_messages"]
        )

        with tf.GradientTape(persistent=True) as tape:
            q_network_losses: Dict[str, NestedArray] = {
                agent: {"policy_loss": tf.zeros(())} for agent in self._agents
            }

            T = actions[self._agents[0]].shape[0]

            state = {agent: core_state[agent][0] for agent in self._agents}
            target_state = {agent: core_state[agent][0] for agent in self._agents}

            message = {agent: core_message[agent][0] for agent in self._agents}
            target_message = {agent: core_message[agent][0] for agent in self._agents}

            # _target_q_networks must be 1 step ahead
            target_channel = self._communication_module.process_messages(target_message)
            for agent in self._agents:
                agent_key = self._agent_net_keys[agent]
                (q_targ, m), s = self._target_q_networks[agent_key](
                    observations[agent].observation[0],
                    target_state[agent],
                    target_channel[agent],
                )
                target_state[agent] = s
                target_message[agent] = m

            for t in range(1, T, 1):
                channel = self._communication_module.process_messages(message)
                target_channel = self._communication_module.process_messages(
                    target_message
                )

                for agent in self._agents:
                    agent_key = self._agent_net_keys[agent]

                    # Cast the additional discount
                    # to match the environment discount dtype.

                    discount = tf.cast(self._discount, dtype=discounts[agent][0].dtype)

                    (q_targ, m), s = self._target_q_networks[agent_key](
                        observations[agent].observation[t],
                        target_state[agent],
                        target_channel[agent],
                    )
                    target_state[agent] = s
                    target_message[agent] = m

                    (q, m), s = self._q_networks[agent_key](
                        observations[agent].observation[t - 1],
                        state[agent],
                        channel[agent],
                    )
                    state[agent] = s
                    message[agent] = m

                    loss, _ = trfl.qlearning(
                        q,
                        actions[agent][t - 1],
                        rewards[agent][t - 1],
                        discount * discounts[agent][t],
                        q_targ,
                    )

                    loss = tf.reduce_mean(loss)
                    q_network_losses[agent]["policy_loss"] += loss

        self._q_network_losses = q_network_losses
        self.tape = tape
