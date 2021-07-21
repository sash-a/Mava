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

"""Variable handling utilities for TensorFlow 2. Adapted from Deepmind's Acme library"""

from concurrent import futures
from typing import Any, Dict, List, Optional

import numpy as np
import tensorflow as tf
from acme.tf import utils as tf2_utils

from mava.systems.tf.variable_sources import VariableSource as MavaVariableSource


class VariableClient:
    """A variable client for updating variables from a remote source."""

    def __init__(
        self,
        client: MavaVariableSource,
        variables: Dict[str, tf.Variable],
        get_keys: List[str] = None,
        set_keys: List[str] = None,
        get_period: int = 1,
        set_period: int = 1,
    ):
        """Initialise the variable server."""
        self._all_keys = list(variables.keys())
        self._get_keys = get_keys if get_keys is not None else self._all_keys
        self._set_keys = set_keys if set_keys is not None else self._all_keys
        self._variables: Dict[str, tf.Variable] = variables
        self._get_call_counter = 0
        self._set_call_counter = 0
        self._get_update_period = get_period
        self._set_update_period = set_period
        self._client = client
        self._request = lambda: client.get_variables(self._get_keys)
        self._request_all = lambda: client.get_variables(self._all_keys)

        self._adjust = lambda: client.set_variables(
            self._set_keys,
            tf2_utils.to_numpy({key: self._variables[key] for key in self._set_keys}),
        )
        # Create a single background thread to fetch variables without necessarily
        # blocking the actor.
        self._executor = futures.ThreadPoolExecutor(max_workers=1)
        self._async_request = lambda: self._executor.submit(self._request)
        self._async_adjust = lambda: self._executor.submit(self._adjust)

        # Initialize this client's future to None to indicate to the `update()`
        # method that there is no pending/running request.
        self._future: Optional[futures.Future] = None

    def get_async(self) -> None:
        """Asynchronously updates the get variables with the latest copy from source."""

        # Track the number of calls (we only update periodically).
        if self._get_call_counter < self._get_update_period:
            self._get_call_counter += 1

        period_reached: bool = self._get_call_counter >= self._get_update_period

        if period_reached and self._future is None:
            # The update period has been reached and no request has been sent yet, so
            # making an asynchronous request now.
            self._future = self._async_request()  # type: ignore

        if self._future is not None and self._future.done():
            # The active request is done so copy the result and remove the future.\
            self._copy(self._future.result())
            self._future = None
            self._get_call_counter = 0

        return

    def set_async(self) -> None:
        """Asynchronously updates source with the set variables."""
        # Track the number of calls (we only update periodically).
        if self._set_call_counter < self._set_update_period:
            self._set_call_counter += 1

        period_reached: bool = self._set_call_counter >= self._set_update_period

        if period_reached and self._future is None:
            # The update period has been reached and no request has been sent yet, so
            # making an asynchronous request now.
            self._future = self._async_adjust()  # type: ignore
            return
        if self._future is not None and self._future.done():
            self._future = None
            self._set_call_counter = 0
        return

    def add_and_wait(self, names: List[str], vars: Dict[str, Any]) -> None:
        """Adds the specified variables to the corresponding variables in source
        and waits for the process to complete before continuing."""
        self._client.add_to_variables(names, vars)

    def get_and_wait(self) -> None:
        """Updates the get variables with the latest copy from source
        and waits for the process to complete before continuing."""
        self._copy(self._request())  # type: ignore
        return

    def get_all_and_wait(self) -> None:
        """Updates all the variables with the latest copy from source
        and waits for the process to complete before continuing."""
        self._copy(self._request_all())  # type: ignore
        return

    def set_and_wait(self) -> None:
        """Updates source with the set variables
        and waits for the process to complete before continuing."""
        self._adjust()  # type: ignore
        return

    def _copy(self, new_variables: Dict[str, Any]) -> None:
        """Copies the new variables to the old ones."""
        for key in new_variables.keys():
            var_type = type(new_variables[key])
            if var_type == dict:
                for agent_key in new_variables[key].keys():
                    for i in range(len(self._variables[key][agent_key])):
                        self._variables[key][agent_key][i].assign(
                            new_variables[key][agent_key][i]
                        )
            elif var_type == np.int32 or var_type == np.float32:
                # TODO (dries): Is this count value getting tracked?
                self._variables[key].assign(new_variables[key])

            elif var_type == tuple:
                for i in range(len(self._variables[key])):
                    self._variables[key][i].assign(new_variables[key][i])
            else:
                NotImplementedError(f"Variable type of {var_type} not implemented.")

        return