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


"""Example running MADQN on debug MPE environments, using a LR schedule."""
import functools
from datetime import datetime
from typing import Any

import launchpad as lp
import sonnet as snt
import tensorflow as tf
from absl import app, flags

from mava.components.tf.modules.exploration import LinearExplorationScheduler
from mava.systems.tf import madqn
from mava.utils import lp_utils
from mava.utils.environments import debugging_utils
from mava.utils.loggers import logger_utils

FLAGS = flags.FLAGS
flags.DEFINE_string(
    "env_name",
    "simple_spread",
    "Debugging environment name (str).",
)
flags.DEFINE_string(
    "action_space",
    "discrete",
    "Environment action space type (str).",
)

flags.DEFINE_string(
    "mava_id",
    str(datetime.now()),
    "Experiment identifier that can be used to continue experiments.",
)
flags.DEFINE_string("base_dir", "~/mava", "Base dir to store experiments.")


def main(_: Any) -> None:

    # Environment.
    environment_factory = functools.partial(
        debugging_utils.make_environment,
        env_name=FLAGS.env_name,
        action_space=FLAGS.action_space,
    )

    # Networks.
    network_factory = lp_utils.partial_kwargs(madqn.make_default_networks)

    # Checkpointer appends "Checkpoints" to checkpoint_dir
    checkpoint_dir = f"{FLAGS.base_dir}/{FLAGS.mava_id}"

    # Log every [log_every] seconds.
    log_every = 0
    logger_factory = functools.partial(
        logger_utils.make_logger,
        directory=FLAGS.base_dir,
        to_terminal=True,
        to_tensorboard=True,
        time_stamp=FLAGS.mava_id,
        time_delta=log_every,
    )

    # LR schedule
    # learning_rate_schedule is a function/class that takes in a trainer timestep t
    # and return the current learning rate.
    # LR that's lr for the first 10001 steps, lr * 0.1 for the next 1000 steps,
    # and lr * 0.01 for any additional steps.
    boundaries = [10000, 11000]
    lr = 1e-2
    values = [lr, lr * 0.1, lr * 0.01]
    learning_rate_schedule = tf.keras.optimizers.schedules.PiecewiseConstantDecay(
        boundaries, values
    )

    # distributed program
    program = madqn.MADQN(
        environment_factory=environment_factory,
        network_factory=network_factory,
        logger_factory=logger_factory,
        num_executors=1,
        exploration_scheduler_fn=LinearExplorationScheduler,
        epsilon_min=0.05,
        epsilon_decay=5e-4,
        importance_sampling_exponent=0.2,
        optimizer=snt.optimizers.Adam(learning_rate=lr),
        checkpoint_subpath=checkpoint_dir,
        learning_rate_schedule=learning_rate_schedule,
    ).build()

    # Ensure only trainer runs on gpu, while other processes run on cpu.
    local_resources = lp_utils.to_device(
        program_nodes=program.groups.keys(), nodes_on_gpu=["trainer"]
    )

    # Launch.
    lp.launch(
        program,
        lp.LaunchType.LOCAL_MULTI_PROCESSING,
        terminal="current_terminal",
        local_resources=local_resources,
    )


if __name__ == "__main__":
    app.run(main)