import copy
import os
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import gzip
import json
from pathlib import Path
import cv2
import uuid

import numpy as np
import torch
import tqdm
from habitat import logger
from habitat.config import read_write
from habitat.config.default import get_agent_config
from habitat.tasks.rearrange.rearrange_sensors import GfxReplayMeasure
from habitat.tasks.rearrange.utils import write_gfx_replay
from habitat.utils.render_wrapper import overlay_frame
from habitat.utils.visualizations.utils import observations_to_image
from habitat_baselines import PPOTrainer
from habitat_baselines.common.baseline_registry import baseline_registry
from habitat_baselines.common.construct_vector_env import construct_envs
from habitat_baselines.common.obs_transformers import \
    apply_obs_transforms_batch
from habitat_baselines.common.tensorboard_utils import TensorboardWriter
from habitat_baselines.rl.ddppo.algo import DDPPO
from habitat_baselines.utils.common import (batch_obs, generate_video,
                                            get_num_actions, inference_mode,
                                            is_continuous_action_space)
from omegaconf import OmegaConf

from goat_bench.utils.utils import write_json

if TYPE_CHECKING:
    from omegaconf import DictConfig

class EpisodeData:
    """根据scene_id和episode_id来获取episode的相关信息，采取lazy loading的方式
    """

    data_path: str

    def __init__(self):
        self.data = {}

    def get(self, scene_id: str, episode_id: str):
        scene_id = os.path.basename(scene_id).split(".")[0]
        episode_id = int(episode_id)

        if scene_id not in self.data:
            with open(f"{self.data_path}/content/{scene_id}.json", 'rb') as f:
                data = json.load(f)
                self.data[scene_id] = data['episodes']
        
        return self.data[scene_id][episode_id]

    def set_data_path(self, data_path: str):
        self.data_path = os.path.dirname(data_path)

episode_data = EpisodeData()
first_episode_key = None

def write_image(image, path: str) -> str:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    filename = path / f"{uuid.uuid4()}.jpg"
    image = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    cv2.imwrite(str(filename), image)
    return filename.name



@baseline_registry.register_trainer(name="goat_ddppo")
@baseline_registry.register_trainer(name="goat_ppo")
class GoatPPOTrainer(PPOTrainer):
    def _eval_checkpoint(
        self,
        checkpoint_path: str,
        writer: TensorboardWriter,
        checkpoint_index: int = 0,
    ) -> None:
        r"""Evaluates a single checkpoint.

        Args:
            checkpoint_path: path of checkpoint
            writer: tensorboard writer object for logging to tensorboard
            checkpoint_index: index of cur checkpoint for logging

        Returns:
            None
        """
        if self._is_distributed:
            raise RuntimeError("Evaluation does not support distributed mode")

        episode_data.set_data_path(self.config.habitat.dataset.data_path)

        # Map location CPU is almost always better than mapping to a CUDA device.
        if self.config.habitat_baselines.eval.should_load_ckpt:
            # ckpt_dict里面存储的主要是模型参数
            ckpt_dict = self.load_checkpoint(
                checkpoint_path, map_location="cpu"
            )
            step_id = ckpt_dict["extra_state"]["step"]
        else:
            ckpt_dict = {"config": None}

        config = self._get_resume_state_config_or_new_config(
            ckpt_dict["config"]
        )

        ppo_cfg = config.habitat_baselines.rl.ppo

        with read_write(config):
            config.habitat.dataset.split = config.habitat_baselines.eval.split

        if (
            len(config.habitat_baselines.video_render_views) > 0
            and len(self.config.habitat_baselines.eval.video_option) > 0
        ):
            agent_config = get_agent_config(config.habitat.simulator)
            # 传感器的配置
            # 文档： https://aihabitat.org/docs/habitat-sim/habitat_sim.sensor.CameraSensorSpec.html
            agent_sensors = agent_config.sim_sensors
            render_view_uuids = [
                agent_sensors[render_view].uuid
                for render_view in config.habitat_baselines.video_render_views
                if render_view in agent_sensors
            ]
            assert len(render_view_uuids) > 0, (
                f"Missing render sensors in agent config: "
                f"{config.habitat_baselines.video_render_views}."
            )
            with read_write(config):
                for render_view_uuid in render_view_uuids:
                    if render_view_uuid not in config.habitat.gym.obs_keys:
                        config.habitat.gym.obs_keys.append(render_view_uuid)
                config.habitat.simulator.debug_render = True

        if config.habitat_baselines.verbose:
            logger.info(f"env config: {OmegaConf.to_yaml(config)}")

        self._init_envs(config, is_eval=True)

        action_space = self.envs.action_spaces[0]
        self.policy_action_space = action_space
        self.orig_policy_action_space = self.envs.orig_action_spaces[0]
        if is_continuous_action_space(action_space):
            # Assume NONE of the actions are discrete
            action_shape = (get_num_actions(action_space),)
            discrete_actions = False
        else:
            # For discrete pointnav
            action_shape = (1,)
            discrete_actions = True

        self._setup_actor_critic_agent(ppo_cfg)

        if self.config.habitat_baselines.should_load_agent_state:
            self.agent.load_state_dict(ckpt_dict["state_dict"])
            logger.info("Loaded agent state from: " + checkpoint_path)
            print("Loading the checkpoint from: " + checkpoint_path)
        self.actor_critic = self.agent.actor_critic

        observations = self.envs.reset()
        batch = batch_obs(observations, device=self.device)
        batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore

        current_episode_reward = torch.zeros(
            self.envs.num_envs, 1, device="cpu"
        )

        test_recurrent_hidden_states = torch.zeros(
            self.config.habitat_baselines.num_environments,
            self.actor_critic.num_recurrent_layers,
            ppo_cfg.hidden_size,
            device=self.device,
        )
        prev_actions = torch.zeros(
            self.config.habitat_baselines.num_environments,
            *action_shape,
            device=self.device,
            dtype=torch.long if discrete_actions else torch.float,
        )
        not_done_masks = torch.zeros(
            self.config.habitat_baselines.num_environments,
            1,
            device=self.device,
            dtype=torch.bool,
        )
        stats_episodes: Dict[
            Any, Any
        ] = {}  # dict of dicts that stores stats per episode
        ep_eval_count: Dict[Any, int] = defaultdict(lambda: 0)

        rgb_frames = [
            [] for _ in range(self.config.habitat_baselines.num_environments)
        ]  # type: List[List[np.ndarray]]
        saved_actions = [
            [] for _ in range(self.config.habitat_baselines.num_environments)
        ]  # type: List[List[np.ndarray]]
        if len(self.config.habitat_baselines.eval.video_option) > 0:
            os.makedirs(self.config.habitat_baselines.video_dir, exist_ok=True)

        number_of_eval_episodes = (
            self.config.habitat_baselines.test_episode_count
        )
        evals_per_ep = self.config.habitat_baselines.eval.evals_per_ep
        if number_of_eval_episodes == -1:
            number_of_eval_episodes = sum(self.envs.number_of_episodes)
        else:
            total_num_eps = sum(self.envs.number_of_episodes)
            # if total_num_eps is negative, it means the number of evaluation episodes is unknown
            if total_num_eps < number_of_eval_episodes and total_num_eps > 1:
                logger.warn(
                    f"Config specified {number_of_eval_episodes} eval episodes"
                    ", dataset only has {total_num_eps}."
                )
                logger.warn(f"Evaluating with {total_num_eps} instead.")
                number_of_eval_episodes = total_num_eps
            else:
                assert evals_per_ep == 1
        assert (
            number_of_eval_episodes > 0
        ), "You must specify a number of evaluation episodes with test_episode_count"

        pbar = tqdm.tqdm(total=number_of_eval_episodes * evals_per_ep)
        self.actor_critic.eval()

        logger.info("Starting eval episodes")

        episode_metrics = []
        eval_info = {} # 导出我们需要的信息

        # Evaluation的主循环
        while (
            len(stats_episodes) < (number_of_eval_episodes * evals_per_ep)
            and self.envs.num_envs > 0
        ):
            # 这里为了方便，我们先固定num_envs=1
            if self.envs.num_envs != 1:
                raise ValueError(
                    f"num_envs should be 1, but got {self.envs.num_envs}"
                )
            # Example:
            # current_episodes_info = [BaseEpisode(episode_id='7', scene_id='data/scene_datasets/hm3d/val//00802-wcojb4TFT35/wcojb4TFT35.basis.glb')]
            current_episodes_info = self.envs.current_episodes()
            episode_key = (
                current_episodes_info[0].scene_id, current_episodes_info[0].episode_id)
            global first_episode_key
            if first_episode_key is None:
                first_episode_key = episode_key
            else:
                if episode_key != first_episode_key:
                    logger.info('调试过程中，只执行一个episode')
                    break

            if episode_key[0] not in eval_info:
                eval_info[episode_key[0]] = {
                }
            if episode_key[1] not in eval_info[episode_key[0]]:
                eval_info[episode_key[0]][episode_key[1]] = {
                    'steps': [],
                    **episode_data.get(*episode_key)
                }
            with inference_mode():
                # 这里的actions包含了过去的动作和即将要执行的动作
                # Example: actions = tensor([[0, 1, 2, 1]])
                # batch则是当前的observation
                # Example: batch = {'compass': tensor([2.1]), 'gps': tensor([1.1, -0.2]), ''  }
                # 
                # 
                (
                    _,
                    actions,
                    _,
                    test_recurrent_hidden_states,
                ) = self.actor_critic.act(
                    batch,
                    test_recurrent_hidden_states,
                    prev_actions,
                    not_done_masks,
                    deterministic=False,
                )
                prev_actions.copy_(actions)  # type: ignore

            if self.config.habitat_baselines.ablate_memory:
                subtask_stop_mask = (
                    (1 - (actions == 6).long())
                    .long()
                    .unsqueeze(-1)
                    .to(test_recurrent_hidden_states.device)
                )
                print(
                    "subtask_stop_mask shape",
                    subtask_stop_mask.shape,
                    test_recurrent_hidden_states.shape,
                )
                test_recurrent_hidden_states = (
                    subtask_stop_mask * test_recurrent_hidden_states
                )
                if (actions == 6).any():
                    print("subtask_stop_mask", subtask_stop_mask, actions)
            # NB: Move actions to CPU.  If CUDA tensors are
            # sent in to env.step(), that will create CUDA contexts
            # in the subprocesses.
            if is_continuous_action_space(self.policy_action_space):
                # Clipping actions to the specified limits
                step_data = [
                    np.clip(
                        a.numpy(),
                        self.policy_action_space.low,
                        self.policy_action_space.high,
                    )
                    for a in actions.cpu()
                ]
            else:
                step_data = [a.item() for a in actions.cpu()]

            outputs = self.envs.step(step_data)

            observations, rewards_l, dones, infos = [
                list(x) for x in zip(*outputs)
            ]
            if self.config.habitat_baselines.debug and actions[0].item() in [
                0,
                6,
            ]:
                print(
                    "action: {} - {}- {} - {}".format(
                        infos,
                        actions,
                        dones,
                        observations[0]["current_subtask"],
                    )
                )
            policy_info = self.actor_critic.get_policy_info(infos, dones)
            for i in range(len(policy_info)):
                infos[i].update(policy_info[i])
            batch = batch_obs(  # type: ignore
                observations,
                device=self.device,
            )
            # batch的图像之后会进行变换，所以这里要备份一下原始的batch
            vis_batch = {k: v.clone() for k, v in batch.items() if "rgb" in k}
            batch = apply_obs_transforms_batch(batch, self.obs_transforms)  # type: ignore


            eval_info[episode_key[0]][episode_key[1]]['steps'].append({
                'rgb': write_image(vis_batch['rgb'][0].cpu().numpy(), 'frames'),
                'action': actions[0].cpu().tolist()[-1],
                'gps': batch['gps'][0].cpu().tolist(),
                'compass': batch['compass'][0].cpu().tolist(),
            })
            
            not_done_masks = torch.tensor(
                [[not done] for done in dones],
                dtype=torch.bool,
                device="cpu",
            )

            rewards = torch.tensor(
                rewards_l, dtype=torch.float, device="cpu"
            ).unsqueeze(1)
            current_episode_reward += rewards
            next_episodes_info = self.envs.current_episodes()
            envs_to_pause = []
            n_envs = self.envs.num_envs
            for i in range(n_envs):
                if (
                    ep_eval_count[
                        (
                            next_episodes_info[i].scene_id,
                            next_episodes_info[i].episode_id,
                        )
                    ]
                    == evals_per_ep
                ):
                    envs_to_pause.append(i)

                saved_actions[i].append(prev_actions[i].item())

                if len(self.config.habitat_baselines.eval.video_option) > 0:
                    # TODO move normalization / channel changing out of the policy and undo it here
                    frame = observations_to_image(
                        {k: v[i] for k, v in vis_batch.items() if "rgb" in k}, {}
                        # {
                        #     "top_down_map": {
                        #         k.split(".")[-1]: v
                        #         for k, v in infos[i].items()
                        #         if "top_down_map" in k
                        #     }
                        # },
                    )
                    if not not_done_masks[i].item():
                        # The last frame corresponds to the first frame of the next episode
                        # but the info is correct. So we use a black frame
                        frame = observations_to_image(
                            {
                                k: v[i] * 0.0
                                for k, v in vis_batch.items()
                                if "rgb" in k
                            }, {}
                            # {
                            #     "top_down_map": {
                            #         k.split(".")[-1]: v
                            #         for k, v in infos[i].items()
                            #         if "top_down_map" in k
                            #     }
                            # },
                        )
                    # frame = overlay_frame(frame, infos[i])
                    rgb_frames[i].append(frame)

                # 如果episode结束了
                if not not_done_masks[i].item():
                    pbar.update()
                    episode_stats = {"reward": current_episode_reward[i].item()}
                    episode_stats.update(
                        self._extract_scalars_from_info(infos[i])
                    )
                    current_episode_reward[i] = 0
                    k = (
                        current_episodes_info[i].scene_id,
                        current_episodes_info[i].episode_id,
                    )
                    ep_eval_count[k] += 1
                    # use scene_id + episode_id as unique id for storing stats
                    stats_episodes[(k, ep_eval_count[k])] = episode_stats

                    episode_state_copy = copy.deepcopy(episode_stats)
                    episode_state_copy["scene_id"] = current_episodes_info[
                        i
                    ].scene_id
                    episode_state_copy["episode_id"] = current_episodes_info[
                        i
                    ].episode_id
                    # episode_state_copy["subtasks"] = current_episodes_info[
                    #     i
                    # ].tasks
                    episode_state_copy["success_by_subtask"] = infos[i][
                        "success.subtask_success"
                    ]

                    eval_info[episode_key[0]][episode_key[1]]['success_by_subtask'] = [bool(i) for i in episode_state_copy["success_by_subtask"]]

                    episode_state_copy["spl_by_subtaskl"] = infos[i][
                        "spl.spl_by_subtask"
                    ]
                    # print("episode_state_copy", current_episodes_info[i].tasks)
                    episode_state_copy["actions"] = saved_actions[i]
                    episode_metrics.append(episode_state_copy)

                    if len(self.config.habitat_baselines.eval.video_option) > 0:
                        generate_video(
                            video_option=self.config.habitat_baselines.eval.video_option,
                            video_dir=self.config.habitat_baselines.video_dir,
                            images=rgb_frames[i],
                            episode_id=current_episodes_info[i].episode_id,
                            checkpoint_idx=checkpoint_index,
                            metrics=self._extract_scalars_from_info(infos[i]),
                            fps=self.config.habitat_baselines.video_fps,
                            tb_writer=writer,
                            keys_to_include_in_name=["success.partial_success"],
                        )

                        rgb_frames[i] = []
                        saved_actions[i] = []

                    gfx_str = infos[i].get(GfxReplayMeasure.cls_uuid, "")
                    if gfx_str != "":
                        write_gfx_replay(
                            gfx_str,
                            self.config.habitat.task,
        #     episode_metrics,
        #     os.path.join(
        #         self.config.habitat_baselines.tensorboard_dir,
        #         "episode_metrics.json",
        #     ),
        # )
                            current_episodes_info[i].episode_id,
                        )

            not_done_masks = not_done_masks.to(device=self.device)
            (
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            ) = self._pause_envs(
                envs_to_pause,
                self.envs,
                test_recurrent_hidden_states,
                not_done_masks,
                current_episode_reward,
                prev_actions,
                batch,
                rgb_frames,
            )

        pbar.close()
        # assert (
        #     len(ep_eval_count) >= number_of_eval_episodes
        # ), f"Expected {number_of_eval_episodes} episodes, got {len(ep_eval_count)}."

        # aggregated_stats = {}
        # for stat_key in next(iter(stats_episodes.values())).keys():
        #     aggregated_stats[stat_key] = np.mean(
        #         [v[stat_key] for v in stats_episodes.values() if stat_key in v]
        #     )

        # for k, v in aggregated_stats.items():
        #     logger.info(f"Average episode {k}: {v:.4f}")

        # step_id = checkpoint_index
        # if "extra_state" in ckpt_dict and "step" in ckpt_dict["extra_state"]:
        #     step_id = ckpt_dict["extra_state"]["step"]

        # writer.add_scalar(
        #     "eval_reward/average_reward", aggregated_stats["reward"], step_id
        # )

        # metrics = {k: v for k, v in aggregated_stats.items() if k != "reward"}
        # for k, v in metrics.items():
        #     writer.add_scalar(f"eval_metrics/{k}", v, step_id)

        # write_json(
        #     episode_metrics,
        #     os.path.join(
        #         self.config.habitat_baselines.tensorboard_dir,
        #         "episode_metrics.json",
        #     ),
        # )

        with open('eval_info.json', 'w') as f:
            json.dump(eval_info, f, indent=4)
        # write_json(
        #     eval_info,
        #     os.path.join(
        #         self.config.habitat_baselines.tensorboard_dir,
        #         "eval_info.json",
        #     ),
        # )

        raise RuntimeError('Debugging中，eval_info已经保存到文件中')
        self.envs.close()
