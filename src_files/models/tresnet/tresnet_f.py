import torch
import torch.nn as nn
from torch.nn import Module as Module
from collections import OrderedDict

from .layers.anti_aliasing import AntiAliasDownsampleLayer
from .layers.avg_pool import FastAvgPool2d
from .layers.frelu import FReLU
from .layers.general_layers import SEModule, SpaceToDepthModule

def conv2d_BN(ni, nf, stride, kernel_size=3):
    return nn.Sequential(
        nn.Conv2d(ni, nf, kernel_size=kernel_size, stride=stride, padding=kernel_size // 2, bias=False),
        nn.BatchNorm2d(nf)
    )


def conv2d_FReLU(ni, nf, stride, kernel_size=3, groups=1):
    return nn.Sequential(
        nn.Conv2d(ni, nf, kernel_size=kernel_size, stride=stride, padding=kernel_size // 2, groups=groups, bias=False),
        nn.BatchNorm2d(nf),
        FReLU(nf),
    )


class BasicBlock(Module):
    expansion = 1

    def __init__(self, inplanes, planes, stride=1, downsample=None, use_se=True, anti_alias_layer=None):
        super(BasicBlock, self).__init__()
        if stride == 1:
            self.conv1 = conv2d_FReLU(inplanes, planes, stride=1)
        else:
            if anti_alias_layer is None:
                self.conv1 = conv2d_FReLU(inplanes, planes, stride=2)
            else:
                self.conv1 = nn.Sequential(conv2d_FReLU(inplanes, planes, stride=1),
                                           anti_alias_layer(channels=planes, filt_size=3, stride=2))

        self.conv2 = conv2d_BN(planes, planes, stride=1)
        self.relu = FReLU(planes)
        self.downsample = downsample
        self.stride = stride
        reduce_layer_planes = max(planes * self.expansion // 4, 64)
        self.se = SEModule(planes * self.expansion, reduce_layer_planes) if use_se else None

    def forward(self, x):
        if self.downsample is not None:
            residual = self.downsample(x)
        else:
            residual = x

        out = self.conv1(x)
        out = self.conv2(out)

        if self.se is not None: out = self.se(out)

        out += residual

        out = self.relu(out)

        return out


class Bottleneck(Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, use_se=True, anti_alias_layer=None):
        super(Bottleneck, self).__init__()
        self.conv1 = conv2d_FReLU(inplanes, planes, kernel_size=1, stride=1)
        if stride == 1:
            self.conv2 = conv2d_FReLU(planes, planes, kernel_size=3, stride=1)
        else:
            if anti_alias_layer is None:
                self.conv2 = conv2d_FReLU(planes, planes, kernel_size=3, stride=2)
            else:
                self.conv2 = nn.Sequential(conv2d_FReLU(planes, planes, kernel_size=3, stride=1),
                                           anti_alias_layer(channels=planes, filt_size=3, stride=2))

        self.conv3 = conv2d_BN(planes, planes * self.expansion, kernel_size=1, stride=1)

        self.relu = FReLU(planes * self.expansion)
        self.downsample = downsample
        self.stride = stride

        reduce_layer_planes = max(planes * self.expansion // 8, 64)
        self.se = SEModule(planes, reduce_layer_planes) if use_se else None

    def forward(self, x):
        if self.downsample is not None:
            residual = self.downsample(x)
        else:
            residual = x

        out = self.conv1(x)
        out = self.conv2(out)
        if self.se is not None: out = self.se(out)

        out = self.conv3(out)
        out = out + residual  # no inplace
        out = self.relu(out)

        return out


class TResNet(Module):

    def __init__(self, layers, in_chans=3, num_classes=1000, width_factor=1.0, first_two_layers=BasicBlock):
        super(TResNet, self).__init__()

        # JIT layers
        space_to_depth = SpaceToDepthModule()
        anti_alias_layer = AntiAliasDownsampleLayer
        global_pool_layer = FastAvgPool2d(flatten=True)

        # TResnet stages
        self.inplanes = int(64 * width_factor)
        self.planes = int(64 * width_factor)
        conv1 = conv2d_FReLU(in_chans * 16, self.planes, stride=1, kernel_size=3)
        layer1 = self._make_layer(first_two_layers, self.planes, layers[0], stride=1, use_se=True,
                                  anti_alias_layer=anti_alias_layer)  # 56x56
        layer2 = self._make_layer(first_two_layers, self.planes * 2, layers[1], stride=2, use_se=True,
                                  anti_alias_layer=anti_alias_layer)  # 28x28
        layer3 = self._make_layer(Bottleneck, self.planes * 4, layers[2], stride=2, use_se=True,
                                  anti_alias_layer=anti_alias_layer)  # 14x14
        layer4 = self._make_layer(Bottleneck, self.planes * 8, layers[3], stride=2, use_se=False,
                                  anti_alias_layer=anti_alias_layer)  # 7x7

        # body
        self.body = nn.Sequential(OrderedDict([
            ('SpaceToDepth', space_to_depth),
            ('conv1', conv1),
            ('layer1', layer1),
            ('layer2', layer2),
            ('layer3', layer3),
            ('layer4', layer4)]))

        # head
        self.global_pool = nn.Sequential(OrderedDict([('global_pool_layer', global_pool_layer)]))
        self.num_features = (self.planes * 8) * Bottleneck.expansion
        fc = nn.Linear(self.num_features, num_classes)
        self.head = nn.Sequential(OrderedDict([('fc', fc)]))

        # model initilization
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='leaky_relu')
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        # residual connections special initialization
        for m in self.modules():
            if isinstance(m, BasicBlock):
                m.conv2[1].weight = nn.Parameter(torch.zeros_like(m.conv2[1].weight))  # BN to zero
            if isinstance(m, Bottleneck):
                m.conv3[1].weight = nn.Parameter(torch.zeros_like(m.conv3[1].weight))  # BN to zero
            if isinstance(m, nn.Linear): m.weight.data.normal_(0, 0.01)

    def _make_layer(self, block, planes, blocks, stride=1, use_se=True, anti_alias_layer=None):
        downsample = None
        if stride != 1 or self.inplanes != planes * block.expansion:
            layers = []
            if stride == 2:
                # avg pooling before 1x1 conv
                layers.append(nn.AvgPool2d(kernel_size=2, stride=2, ceil_mode=True, count_include_pad=False))
            layers += [conv2d_BN(self.inplanes, planes * block.expansion, kernel_size=1, stride=1)]
            downsample = nn.Sequential(*layers)

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, use_se=use_se,
                            anti_alias_layer=anti_alias_layer))
        self.inplanes = planes * block.expansion
        for i in range(1, blocks): layers.append(
            block(self.inplanes, planes, use_se=use_se, anti_alias_layer=anti_alias_layer))
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.body(x)
        self.embeddings = self.global_pool(x)
        logits = self.head(self.embeddings)
        return logits


def TResnetS(model_params):
    """Constructs a small TResnet model.
    """
    in_chans = 3
    num_classes = model_params['num_classes']
    args = model_params['args']
    model = TResNet(layers=[3, 4, 6, 3], num_classes=num_classes, in_chans=in_chans)
    return model

#Flops:47.50G, params:55.16M
def TResnetM(model_params):
    """Constructs a medium TResnet model.
    """
    in_chans = 3
    num_classes = model_params['num_classes']
    model = TResNet(layers=[3, 4, 11, 3], num_classes=num_classes, in_chans=in_chans)
    return model

#Flops:67.70G, params:64.67M
def TResnetD(model_params):
    """Constructs a large TResnet model.
    """
    in_chans = 3
    num_classes = model_params['num_classes']
    layers_list = [3, 6, 14, 3]
    model = TResNet(layers=layers_list, num_classes=num_classes, in_chans=in_chans, first_two_layers=Bottleneck)
    return model

#Flops:73.07G, params:70.01M
def TResnetL(model_params):
    """Constructs a large TResnet model.
    """
    in_chans = 3
    num_classes = model_params['num_classes']
    layers_list = [3, 4, 23, 3]
    model = TResNet(layers=layers_list, num_classes=num_classes, in_chans=in_chans, first_two_layers=Bottleneck)
    return model

def TResnetXL(model_params):
    """Constructs a large TResnet model.
    """
    in_chans = 3
    num_classes = model_params['num_classes']
    layers_list = [3, 8, 34, 5]
    model = TResNet(layers=layers_list, num_classes=num_classes, in_chans=in_chans, first_two_layers=Bottleneck)
    return model