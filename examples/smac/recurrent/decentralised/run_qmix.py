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

"""Example running recurrent MADQN on multi-agent Starcraft 2 (SMAC) environment."""

import functools
from datetime import datetime
from typing import Any, Dict, Mapping, Sequence, Union

import launchpad as lp
from mava.components.tf.modules import mixing
from mava.utils.enums import ArchitectureType
import sonnet as snt
import tensorflow as tf
from absl import app, flags
from acme import types

from mava import specs as mava_specs
from mava.components.tf import networks
from mava.components.tf.modules.exploration.exploration_scheduling import (
    LinearExplorationTimestepScheduler,
)
from mava.components.tf.networks.epsilon_greedy import EpsilonGreedy
from mava.systems.tf import madqn
from mava.utils import lp_utils
from mava.utils.environments import smac_utils
from mava.utils.loggers import logger_utils

FLAGS = flags.FLAGS
flags.DEFINE_string(
    "map_name",
    "3m",
    "Starcraft 2 micromanagement map name (str).",
)

flags.DEFINE_string(
    "mava_id",
    str(datetime.now()),
    "Experiment identifier that can be used to continue experiments.",
)
flags.DEFINE_string("base_dir", "~/mava", "Base dir to store experiments.")


def custom_recurrent_network(
    environment_spec: mava_specs.MAEnvironmentSpec,
    agent_net_keys: Dict[str, str],
    q_networks_layer_sizes: Union[Dict[str, Sequence], Sequence] = [128, 128],
) -> Mapping[str, types.TensorTransformation]:
    """Creates networks used by the agents."""

    specs = environment_spec.get_agent_specs()

    # Create agent_type specs
    specs = {agent_net_keys[key]: specs[key] for key in specs.keys()}

    if isinstance(q_networks_layer_sizes, Sequence):
        q_networks_layer_sizes = {key: q_networks_layer_sizes for key in specs.keys()}

    q_networks = {}
    action_selectors = {}
    for key in specs.keys():

        # Get total number of action dimensions from action spec.
        num_dimensions = specs[key].actions.num_values

        # Create the policy network.
        # q_network = snt.DeepRNN(
        #     [
        #         snt.Linear(q_networks_layer_sizes[key][0]),
        #         tf.nn.relu,
        #         snt.GRU(q_networks_layer_sizes[key][1]),
        #         networks.NearZeroInitializedLinear(num_dimensions),
        #     ]
        # )
        q_network = snt.DeepRNN(
            [snt.Linear(64), tf.nn.relu, snt.GRU(64), snt.Linear(num_dimensions)]
        )

        # epsilon greedy action selector
        action_selector = EpsilonGreedy

        q_networks[key] = q_network
        action_selectors[key] = action_selector

    return {
        "q_networks": q_networks,
        "action_selectors": action_selectors,
    }


def main(_: Any) -> None:

    # environment
    environment_factory = functools.partial(
        smac_utils.make_environment, map_name=FLAGS.map_name
    )

    # Networks.
    network_factory = lp_utils.partial_kwargs(
        custom_recurrent_network,
    )

    # Checkpointer appends "Checkpoints" to checkpoint_dir
    checkpoint_dir = f"{FLAGS.base_dir}/{FLAGS.mava_id}"

    # Log every [log_every] seconds.
    log_every = 0.0
    logger_factory = functools.partial(
        logger_utils.make_logger,
        directory=FLAGS.base_dir,
        to_terminal=True,
        to_tensorboard=True,
        time_stamp=FLAGS.mava_id,
        time_delta=log_every,
    )

    # distributed program
    program = madqn.RecurrentMADQN(
        environment_factory=environment_factory,
        network_factory=network_factory,
        logger_factory=logger_factory,
        num_executors=1,
        checkpoint_subpath=checkpoint_dir,
        exploration_scheduler_fn=LinearExplorationTimestepScheduler(
            epsilon_start=1.0, epsilon_min=0.05, epsilon_decay_steps=50000
        ),
        optimizer=snt.optimizers.RMSProp(
            learning_rate=0.0005, epsilon=0.00001, decay=0.99
        ),
        batch_size=32,
        executor_variable_update_period=100,
        target_update_period=200,
        max_gradient_norm=10.0,
        sequence_length=61,
        period=61,
        mixer=mixing.MonotonicMixing,
    ).build()
    # program = madqn.MADQN(
    #     environment_factory=environment_factory,
    #     network_factory=network_factory,
    #     logger_factory=logger_factory,
    #     num_executors=1,
    #     exploration_scheduler_fn=LinearExplorationTimestepScheduler(
    #         epsilon_start=1.0, epsilon_min=0.05, epsilon_decay_steps=50000
    #     ),
    #     optimizer=snt.optimizers.RMSProp(
    #         learning_rate=0.0005, epsilon=0.00001, decay=0.99
    #     ),
    #     checkpoint_subpath=checkpoint_dir,
    #     batch_size=32,
    #     executor_variable_update_period=100,
    #     target_update_period=200,
    #     max_gradient_norm=10.0,
    #     trainer_fn=madqn.training.MADQNRecurrentTrainer,
    #     executor_fn=madqn.execution.MADQNRecurrentExecutor,
    # ).build()

    # launch
    local_resources = lp_utils.to_device(
        program_nodes=program.groups.keys(), nodes_on_gpu=["trainer"]
    )
    lp.launch(
        program,
        lp.LaunchType.LOCAL_MULTI_PROCESSING,
        # terminal="current_terminal",
        local_resources=local_resources,
    )


if __name__ == "__main__":
    app.run(main)