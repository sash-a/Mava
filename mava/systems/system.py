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

"""Mava system implementation."""

from typing import Any, Dict, List, Tuple, Optional, Iterator

import acme
import launchpad as lp
import reverb
import sonnet as snt


import mava
from mava import adders
from mava.core import SystemBuilder
from mava.callbacks import Callback
from mava.systems.tf.variable_sources import VariableSource as MavaVariableSource
from mava.systems.callback_hook import SystemCallbackHookMixin


class System(SystemBuilder, SystemCallbackHookMixin):
    """MARL system."""

    def __init__(
        self,
        config: Dict[str, Dict[str, Any]],
        components: Dict[str, Dict[str, Callback]],
    ):
        """[summary]

        Args:
            config (Dict[str, Dict[str, Any]]): [description]
            components (Dict[str, Dict[str, Callback]]): [description]
        """

        self.config = config
        self.components = components

        self.callbacks = []
        for system_components in components.values():
            for component in system_components.values():
                self.callbacks.append(component)

        self.on_building_init_start(self)

        self.on_building_init(self)

        self.on_building_init_end(self)

    def tables(self) -> List[reverb.Table]:
        """ "Create tables to insert data into.
        Args:
            environment_spec (specs.MAEnvironmentSpec): description of the action and
                observation spaces etc. for each agent in the system.
        Raises:
            NotImplementedError: unknown executor type.
        Returns:
            List[reverb.Table]: a list of data tables for inserting data.
        """

        # start of make replay tables
        self.on_building_tables_start(self)

        # make adder signature
        self.on_building_tables_adder_signature(self)

        # make rate limiter
        self.on_building_tables_rate_limiter(self)

        # make tables
        self.on_building_tables(self)

        # end of make replay tables
        self.on_building_tables_end(self)

        return self.tables

    def dataset(
        self,
        replay_client: reverb.Client,
        table_name: str,
    ) -> Iterator[reverb.ReplaySample]:
        """Create a dataset iterator to use for training/updating the system.
        Args:
            replay_client (reverb.Client): Reverb Client which points to the
                replay server.
        Returns:
            [type]: dataset iterator.
        Yields:
            Iterator[reverb.ReplaySample]: data samples from the dataset.
        """
        self._replay_client = replay_client
        self._table_name = table_name

        # start of make dataset iterator
        self.on_building_make_dataset_iterator_start(self)

        # make dataset
        self.on_building_dataset(self)

        # end of make dataset iterator
        self.on_building_make_dataset_iterator_end(self)

        return self.dataset

    def adder(
        self,
        replay_client: reverb.Client,
    ) -> Optional[adders.ParallelAdder]:
        """Create an adder which records data generated by the executor/environment.
        Args:
            replay_client (reverb.Client): Reverb Client which points to the
                replay server.
        Raises:
            NotImplementedError: unknown executor type.
        Returns:
            Optional[adders.ParallelAdder]: adder which sends data to a replay buffer.
        """
        self._replay_client = replay_client

        # start of make make adder
        self.on_building_adder_start(self)

        # make adder signature
        self.on_building_adder_priority(self)

        # make rate limiter
        self.on_building_adder(self)

        # end of make make adder
        self.on_building_adder_end(self)

        return self.adder

    def system(
        self,
    ) -> Tuple[Dict[str, Dict[str, snt.Module]], Dict[str, Dict[str, snt.Module]]]:
        """Initialise the system variables from the network factory."""

        self.on_building_system_start(self)

        self.on_building_system_networks(self)

        self.on_building_system_architecture(self)

        self.on_building_system(self)

        self.on_building_system_end(self)

        return self.system_networks

    def variable_server(self) -> MavaVariableSource:
        """Create the variable server.
        Args:
            networks (Dict[str, Dict[str, snt.Module]]): dictionary with the
            system's networks in.
        Returns:
            variable_source (MavaVariableSource): A Mava variable source object.
        """

        # start of make variable server
        self.on_building_variable_server_start(self)

        # make variable server
        self.on_building_variable_server(self)

        # end of make variable server
        self.on_building_variable_server_end(self)

        return self.variable_server

    def executor(
        self,
        executor_id: str,
        replay_client: reverb.Client,
        variable_source: acme.VariableSource,
    ) -> mava.ParallelEnvironmentLoop:
        """System executor
        Args:
            executor_id (str): id to identify the executor process for logging purposes.
            replay (reverb.Client): replay data table to push data to.
            variable_source (acme.VariableSource): variable server for updating
                network variables.
            counter (counting.Counter): step counter object.
        Returns:
            mava.ParallelEnvironmentLoop: environment-executor loop instance.
        """

        self._executor_id = executor_id
        self._replay_client = replay_client
        self._variable_source = variable_source

        self.on_building_executor_start(self)

        self.on_building_executor_logger(self)

        self.on_building_executor(self)

        self.on_building_executor_train_loop(self)

        self.on_building_executor_end(self)

        return self.train_loop

    def evaluator(
        self,
        variable_source: acme.VariableSource,
    ) -> Any:
        """System evaluator (an executor process not connected to a dataset)
        Args:
            variable_source (acme.VariableSource): variable server for updating
                network variables.
            counter (counting.Counter): step counter object.
        Returns:
            Any: environment-executor evaluation loop instance for evaluating the
                performance of a system.
        """

        self._variable_source = variable_source

        self.on_building_evaluator_start(self)

        self.on_building_evaluator_logger(self)

        self.on_building_evaluator(self)

        self.on_building_evaluator_eval_loop(self)

        self.on_building_evaluator_end(self)

        return self.eval_loop

    def trainer(
        self,
        trainer_id: str,
        replay_client: reverb.Client,
        variable_source: MavaVariableSource,
    ) -> mava.core.Trainer:
        """System trainer
        Args:
            replay (reverb.Client): replay data table to pull data from.
            counter (counting.Counter): step counter object.
        Returns:
            mava.core.Trainer: system trainer.
        """

        self._trainer_id = trainer_id
        self._replay_client = replay_client
        self._variable_source = variable_source

        self.on_building_evaluator_start(self)

        self.on_building_evaluator_logger(self)

        self.on_building_evaluator(self)

        self.on_building_evaluator_eval_loop(self)

        self.on_building_evaluator_end(self)

        return self.trainer

    def distributor(self) -> Any:
        """Build the distributed system as a graph program.
        Args:
            name (str, optional): system name. Defaults to "maddpg".
        Returns:
            Any: graph program for distributed system training.
        """
        name = self.config.system_name
        self.program = lp.Program(name=name)

        self.on_building_distributor_start(self)

        self.on_building_distributor_tables(self)

        self.on_building_distributor_variable_server(self)

        self.on_building_distributor_trainer(self)

        self.on_building_distributor_evaluator(self)

        self.on_building_distributor_executor(self)

        self.on_building_distributor_end(self)

        return self.program
