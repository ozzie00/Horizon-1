from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import argparse
import collections
import json
import sys

import numpy as np

from caffe2.proto import caffe2_pb2
from caffe2.python import core

from ml.rl.test.gym.open_ai_gym_environment import EnvType, ModelType,\
    OpenAIGymEnvironment
from ml.rl.training.ddpg_trainer import DDPGTrainer
from ml.rl.training.continuous_action_dqn_trainer import ContinuousActionDQNTrainer
from ml.rl.training.discrete_action_trainer import DiscreteActionTrainer
from ml.rl.training.conv.discrete_action_conv_trainer import DiscreteActionConvTrainer
from ml.rl.thrift.core.ttypes import RLParameters, TrainingParameters,\
    DiscreteActionModelParameters, DiscreteActionConvModelParameters,\
    CNNModelParameters, ContinuousActionModelParameters, KnnParameters,\
    DDPGNetworkParameters, DDPGTrainingParameters, DDPGModelParameters

import logging
logger = logging.getLogger(__name__)

USE_CPU = -1

EnvDetails = collections.namedtuple(
    'EnvDetails',
    ['state_dim', 'action_dim', 'action_range'])


def run(
    gym_env,
    model_type,
    trainer,
    test_run_name,
    score_bar,
    num_episodes=301,
    max_steps=None,
    train_every_ts=100,
    train_after_ts=10,
    test_every_ts=100,
    test_after_ts=10,
    num_train_batches=10,
    avg_over_num_episodes=100,
    render=False,
    render_every=10,
):
    avg_reward_history = []

    predictor = trainer.predictor()
    total_timesteps = 0

    for i in range(num_episodes):
        terminal = False
        next_state = gym_env.env.reset()
        next_action = gym_env.policy(predictor, next_state, False)
        reward_sum = 0
        ep_timesteps = 0

        if model_type == ModelType.CONTINUOUS_ACTION.value:
            predictor.noise.clear()

        while not terminal:
            state = next_state
            action = next_action

            if render:
                gym_env.env.render()

            if gym_env.action_type == EnvType.DISCRETE_ACTION:
                action_index = np.argmax(action)
                next_state, reward, terminal, _ = gym_env.env.step(action_index)
            else:
                next_state, reward, terminal, _ = gym_env.env.step(action)

            ep_timesteps += 1
            total_timesteps += 1
            next_action = gym_env.policy(predictor, next_state, False)
            reward_sum += reward

            if model_type == ModelType.DISCRETE_ACTION.value:
                possible_next_actions = [
                    0 if terminal else 1 for __ in range(gym_env.action_dim)
                ]
                possible_next_actions_lengths = gym_env.action_dim
            elif model_type == ModelType.PARAMETRIC_ACTION.value:
                if terminal:
                    possible_next_actions = np.array([])
                    possible_next_actions_lengths = 0
                else:
                    possible_next_actions = np.eye(gym_env.action_dim)
                    possible_next_actions_lengths = gym_env.action_dim
            elif model_type == ModelType.CONTINUOUS_ACTION.value:
                possible_next_actions = None
                possible_next_actions_lengths = None

            gym_env.insert_into_memory(
                np.float32(state), action, np.float32(reward),
                np.float32(next_state), next_action, terminal,
                possible_next_actions, possible_next_actions_lengths
            )

            # Training loop
            if (
                total_timesteps % train_every_ts == 0 and
                total_timesteps > train_after_ts and
                len(gym_env.replay_memory) >= trainer.minibatch_size
            ):
                for _ in range(num_train_batches):
                    if model_type == ModelType.CONTINUOUS_ACTION.value:
                        samples = gym_env.sample_memories(trainer.minibatch_size)
                        trainer.train(predictor, samples)
                    else:
                        gym_env.sample_and_load_training_data_c2(
                            trainer.minibatch_size, model_type, trainer.maxq_learning)
                        trainer.train(reward_timelines=None, evaluator=None)

            # Evaluation loop
            if (
                total_timesteps % test_every_ts == 0 and
                total_timesteps > test_after_ts
            ):
                avg_rewards = gym_env.run_ep_n_times(
                    avg_over_num_episodes, predictor, test=True)
                avg_reward_history.append(avg_rewards)
                logger.info(
                    "Achieved an average reward score of {} over {} evaluations."
                    " Total episodes: {}, total timesteps: {}."
                    .format(avg_rewards, avg_over_num_episodes, i + 1, total_timesteps)
                )
                if score_bar is not None and avg_rewards > score_bar:
                    logger.info('Avg. reward history for {}: {}'.format(
                        test_run_name, avg_reward_history))
                    return avg_reward_history

            if max_steps and ep_timesteps >= max_steps:
                break

        # Always eval on last episode if previous eval loop didn't return.
        if i == num_episodes - 1:
            avg_rewards = gym_env.run_ep_n_times(
                avg_over_num_episodes, predictor, test=True)
            avg_reward_history.append(avg_rewards)
            logger.info(
                "Achieved an average reward score of {} over {} evaluations."
                " Total episodes: {}, total timesteps: {}."
                .format(avg_rewards, avg_over_num_episodes, i + 1, total_timesteps)
            )

    logger.info('Avg. reward history for {}: {}'.format(
        test_run_name, avg_reward_history))
    return avg_reward_history


def main(args):
    parser = argparse.ArgumentParser(
        description="Train a RL net to play in an OpenAI Gym environment."
    )
    parser.add_argument(
        "-p",
        "--parameters",
        help="Path to JSON parameters file.",
    )
    parser.add_argument(
        "-s",
        "--score-bar",
        help="Bar for averaged tests scores.",
        type=float,
        default=None,
    )
    parser.add_argument(
        "-g",
        "--gpu_id",
        help="If set, will use GPU with specified ID. Otherwise will use CPU.",
        default=USE_CPU,
    )
    parser.add_argument(
        "-l",
        "--log_level",
        help="If set, use logging level specified (debug, info, warning, error, "
        "critical). Else defaults to info.",
        default='info',
    )
    args = parser.parse_args(args)

    if args.log_level not in ('debug', 'info', 'warning', 'error', 'critical'):
        raise Exception("Logging level {} not valid level.".format(args.log_level))
    else:
        logger.setLevel(getattr(logging, args.log_level.upper()))

    with open(args.parameters, 'r') as f:
        params = json.load(f)

    return run_gym(params, args.score_bar, args.gpu_id)


def run_gym(params, score_bar, gpu_id):
    rl_settings = params['rl']

    env_type = params['env']
    env = OpenAIGymEnvironment(env_type, rl_settings['epsilon'],
        rl_settings['softmax_policy'])
    model_type = params['model_type']
    c2_device = core.DeviceOption(
        caffe2_pb2.CPU if gpu_id == USE_CPU else caffe2_pb2.CUDA,
        gpu_id,
    )

    if model_type == ModelType.DISCRETE_ACTION.value:
        with core.DeviceScope(c2_device):
            training_settings = params['training']
            trainer_params = DiscreteActionModelParameters(
                actions=env.actions,
                rl=RLParameters(**rl_settings),
                training=TrainingParameters(**training_settings)
            )
            if env.img:
                trainer = DiscreteActionConvTrainer(
                    DiscreteActionConvModelParameters(
                        fc_parameters=trainer_params,
                        cnn_parameters=CNNModelParameters(**params['cnn']),
                        num_input_channels=env.num_input_channels,
                        img_height=env.height,
                        img_width=env.width
                    ),
                    env.normalization,
                )
            else:
                trainer = DiscreteActionTrainer(
                    trainer_params,
                    env.normalization,
                )
    elif model_type == ModelType.PARAMETRIC_ACTION.value:
        with core.DeviceScope(c2_device):
            training_settings = params['training']
            trainer_params = ContinuousActionModelParameters(
                rl=RLParameters(**rl_settings),
                training=TrainingParameters(**training_settings),
                knn=KnnParameters(model_type='DQN', ),
            )
            trainer = ContinuousActionDQNTrainer(
                trainer_params,
                env.normalization,
                env.normalization_action
            )
    elif model_type == ModelType.CONTINUOUS_ACTION.value:
            training_settings = params['shared_training']
            actor_settings = params['actor_training']
            critic_settings = params['critic_training']
            trainer_params = DDPGModelParameters(
                rl=RLParameters(**rl_settings),
                shared_training=DDPGTrainingParameters(**training_settings),
                actor_training=DDPGNetworkParameters(**actor_settings),
                critic_training=DDPGNetworkParameters(**critic_settings),
            )
            trainer = DDPGTrainer(trainer_params, EnvDetails(
                state_dim=env.state_dim,
                action_dim=env.action_dim,
                action_range=(env.action_space.low, env.action_space.high),
            ))
    else:
        raise NotImplementedError(
            "Model of type {} not supported".format(model_type))

    return run(
        env, model_type, trainer, "{} test run".format(env_type), score_bar,
        **params["run_details"]
    )


if __name__ == '__main__':
    args = sys.argv
    if len(args) not in [3, 5, 7, 9]:
        raise Exception(
            "Usage: python run_gym.py -p <parameters_file>" +
            " [-s <score_bar>] [-g <gpu_id>] [-l <log_level>]"
        )
    main(args[1:])