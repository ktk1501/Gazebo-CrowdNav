import torch
import torch.nn as nn
from torch.nn.functional import softmax
import numpy as np
import logging
from gym_crowd.envs.utils.action import ActionRot, ActionXY
from dynav.policy.utils import reward
from dynav.policy.cadrl import CADRL, ValueNetwork as VN


class ValueNetwork(nn.Module):
    def __init__(self, input_dim, kinematics, mlp1_dims, mlp2_dims):
        super().__init__()
        self.input_dim = input_dim
        self.kinematics = kinematics
        self.mlp1 = nn.Sequential(nn.Linear(input_dim, mlp1_dims[0]), nn.ReLU(),
                                  nn.Linear(mlp1_dims[0], mlp1_dims[1]), nn.ReLU(),
                                  nn.Linear(mlp1_dims[1], mlp1_dims[2]), nn.ReLU(),
                                  nn.Linear(mlp1_dims[2], mlp2_dims))
        self.mlp2 = nn.Sequential(nn.Linear(mlp2_dims, 1))
        self.attention = nn.Sequential(nn.Linear(mlp2_dims, 1))
        self.attention_weights = None

    def forward(self, state):
        """
        First transform the world coordinates to self-centric coordinates and then do forward computation

        :param state: tensor of shape (batch_size, # of peds, length of a joint state)
        :return:
        """
        size = state.shape
        state = VN.rotate(torch.reshape(state, (-1, size[2])), self.kinematics)
        mlp1_output = self.mlp1(state)
        scores = torch.reshape(self.attention(mlp1_output), (size[0], size[1], 1)).squeeze(dim=2)
        weights = softmax(scores, dim=1).unsqueeze(2)
        # for visualization purpose
        self.attention_weights = weights[0, :, 0].data.cpu().numpy()
        features = torch.reshape(mlp1_output, (size[0], size[1], -1))
        weighted_feature = torch.sum(weights.expand_as(features) * features, dim=1)
        value = self.mlp2(weighted_feature)
        return value


class SARL(CADRL):
    def __init__(self):
        super().__init__()

    def configure(self, config):
        self.gamma = config.getfloat('rl', 'gamma')

        self.kinematics = config.get('action_space', 'kinematics')
        self.sampling = config.get('action_space', 'sampling')
        self.speed_samples = config.getint('action_space', 'speed_samples')
        self.rotation_samples = config.getint('action_space', 'rotation_samples')

        input_dim = config.getint('sarl', 'input_dim')
        mlp1_dims = [int(x) for x in config.get('sarl', 'mlp1_dims').split(', ')]
        mlp2_dims = config.getint('sarl', 'mlp2_dims')
        self.model = ValueNetwork(input_dim, self.kinematics, mlp1_dims, mlp2_dims)
        self.multiagent_training = config.getboolean('sarl', 'multiagent_training')
        logging.info('SARL: {} agent training'.format('single' if not self.multiagent_training else 'multiple'))

    def predict(self, state):
        """
        Input state is the joint state of navigator concatenated by the observable state of other agents

        To predict the best action, agent samples actions and propagates one step to see how good the next state is
        thus the reward function is needed

        """
        if self.phase is None or self.device is None:
            raise AttributeError('Phase, device attributes have to be set!')
        if self.phase == 'train' and self.epsilon is None:
            raise AttributeError('Epsilon attribute has to be set in training phase')

        if self.reach_destination(state):
            return ActionXY(0, 0)
        self.build_action_space(state.self_state.v_pref)

        probability = np.random.random()
        if self.phase == 'train' and probability < self.epsilon:
            max_action = self.action_space[np.random.choice(len(self.action_space))]
        else:
            max_value = float('-inf')
            max_action = None
            for action in self.action_space:
                batch_next_states = []
                for ped_state in state.ped_states:
                    next_self_state = self.propagate(state.self_state, action)
                    next_ped_state = self.propagate(ped_state, ActionXY(ped_state.vx, ped_state.vy))
                    next_dual_state = torch.Tensor([next_self_state + next_ped_state]).to(self.device)
                    batch_next_states.append(next_dual_state)
                batch_next_states = torch.cat(batch_next_states, dim=0).unsqueeze(0)
                value = reward(state, action, self.kinematics, self.time_step) + \
                    pow(self.gamma, state.self_state.v_pref) * self.model(batch_next_states).data.item()
                if value > max_value:
                    max_value = value
                    max_action = action

        if self.phase == 'train':
            self.last_state = self.transform(state)

        return max_action

    def transform(self, state):
        """
        Take the state passed from agent and transform it to tensor for batch training

        :param state:
        :return: tensor of shape (# of peds, len(state))
        """
        return torch.cat([torch.Tensor([state.self_state + ped_state]).to(self.device)
                          for ped_state in state.ped_states], dim=0)

    def get_attention_weights(self):
        return self.model.attention_weights