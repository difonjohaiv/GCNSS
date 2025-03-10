from __future__ import division
from __future__ import print_function
import optuna
import random

import time
import argparse
import numpy as np
from copy import deepcopy as dcp
import scipy.sparse as sp
import math

import os.path as osp
import os
from torch_geometric.utils import dropout_adj, to_dense_adj, to_scipy_sparse_matrix, add_self_loops, dense_to_sparse

import torch
import torch.nn.functional as F
import torch.optim as optim

from utils import load_data, accuracy, sparse_mx_to_torch_sparse_tensor, normalize, label_propagation, drop_feature, aug_random_mask, adj_nor
from models import GCN, MLP

from torch_geometric.datasets import Planetoid, CitationFull,WikiCS, Coauthor, Amazon
import torch_geometric as pyg
import torch_geometric.transforms as T
from torch.autograd import Variable


# Training settings
parser = argparse.ArgumentParser()
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='Disables CUDA training.')
parser.add_argument('--fastmode', action='store_true', default=False,
                    help='Validate during training pass.')
parser.add_argument('--data_aug', type=int, default=1,
                    help='do data augmentation.')
parser.add_argument('--kk', type=int, default=1,
                    help='y_pre select k')
parser.add_argument('--sample_size', type=float, default=0.6,
                    help='sample size')
parser.add_argument('--neg_type', type=float, default=0,
                    help='0,selection;1 not selection')
parser.add_argument('--encoder_type', type=int, default=3,
                    help='do data augmentation.')
parser.add_argument('--debias', type=float, default=0.12,
                    help='debias rate.')
parser.add_argument('--seed', type=int, default=42, help='Random seed.')
parser.add_argument('--epochs', type=int, default=200,
                    help='Number of epochs to train.')
parser.add_argument('--lr', type=float, default=0.005,
                    help='Initial learning rate.')
parser.add_argument('--weight', type=float, default=0.,
                    help='Initial loss rate.')
parser.add_argument('--weight_decay', type=float, default=5e-4,
                    help='Weight decay (L2 loss on parameters).')
parser.add_argument('--hidden', type=int, default=128,
                    help='Number of hidden units.')
parser.add_argument('--dropout', type=float, default=0.5,
                    help='Dropout rate (1 - keep probability).')
parser.add_argument('--tau', type=float, default=0.4,
                    help='tau rate .')
parser.add_argument('--dataset', type=str, default='Cora',
                    help='Cora/CiteSeer/PubMed/')
parser.add_argument('--encoder', type=str, default='GCN',
                    help='GCN/SGC/GAT/')

args = parser.parse_args()
times = 10

#load dataset
def get_dataset(path, name):
    assert name in ['Cora', 'CiteSeer', 'PubMed', 'DBLP','WikiCS','Amazon-Photo']
    name = 'dblp' if name == 'DBLP' else name
    print(name)
    return (CitationFull if name == 'dblp' else Planetoid)(path,name,transform=T.NormalizeFeatures())

if args.dataset=='Cora' or args.dataset=='CiteSeer' or args.dataset=='PubMed':
    path = osp.join(osp.expanduser('~'), 'datasets', args.dataset)
    print(path)
    print("hhhh")
else:
    path = osp.expanduser('~/datasets')
    path = osp.join(path, args.dataset)
dataset = get_dataset(path, args.dataset)    
data = dataset[0]
#data.edge_index, _  = pyg.utils.add_self_loops(data.edge_index)


#######数据处理
idx_train = data.train_mask  # 140个训练样本
idx_val = data.val_mask  # 500个验证
idx_test = data.test_mask  # 1000个测试样本
features = data.x
features = normalize(features)  # 归一化后的结果是ndarray
features = torch.from_numpy(features)  # 转成张量
labels = data.y
adj = torch.eye(data.x.shape[0])  # eye生成对角线全1，其余位置全0的方阵
for i in range(data.edge_index.shape[1]):
    adj[data.edge_index[0][i]][data.edge_index[1][i]] = 1  # 把一个点到点的邻接矩阵转变成二维邻接矩阵
adj = adj.float()
adj = adj_nor(adj)


best_model = None
best_val_acc = 0.0

#####训练过程
#####encoder_type GNN models与CL结合的方式;data_aug 是否使用数据增强; 
#####neg_type,是否使用负例选择策略;kk,负例选择过程，我们认为预测的topk个类别为节点可能属于的类别;
#####sample_size,采样加入对比学习节点的数量;
def train(model, optimizer, epoch, features, adj, idx_train, idx_val, labels, data_aug, encoder_type, debias, kk, sample_size, neg_type):
    global best_model
    global best_val_acc
    t = time.time()
    model.train()
    optimizer.zero_grad()
    
    #semi-supervised CE loss
    y_pre, _ = model(features, adj, encoder_type)
    loss_train = F.nll_loss(y_pre[idx_train], labels[idx_train])  # 利用train-set计算entropy-cross损失
    acc_train = accuracy(y_pre[idx_train], labels[idx_train])  # 测量准确率
    
    #sample nodes 
    node_mask = torch.empty(features.shape[0],dtype=torch.float32).uniform_(0,1).cpu()  # 创建了一个随机数（dim=2708）
    node_mask = node_mask < sample_size  # 对于小于sample_size==0.6的设置为true，否则为false。node_mask为参与对比学习的节点
    
    #negative selection, neg_mask
    if neg_type == 0:  # 采用negative selecting strategy
        y_pre = y_pre.detach()
        y_pre = y_pre[node_mask]
        
        _, y_poslabel = torch.topk(y_pre, kk)
        y_pl = torch.zeros(y_pre.shape).cpu()
        y_pl = y_pl.scatter_(1, y_poslabel, 1)
        neg_mask = torch.mm(y_pl, y_pl.T) <= 0
        neg_mask = neg_mask.cpu()
        
        del y_pl, y_poslabel
        torch.cuda.empty_cache()
    else :
        neg_mask = (1 - torch.eye(node_mask.sum())).cpu()
    
    if data_aug == 1:
        #features1 = aug_random_mask(features, 0.3)
        #features2 = aug_random_mask(features, 0.4)
        features1 = drop_feature(features, 0.3)  # data-aug
        features2 = drop_feature(features, 0.4)

        _, output1 = model(features1, adj, encoder_type)
        _, output2 = model(features2, adj, encoder_type)
        
        del features1, features2
        torch.cuda.empty_cache()
        
        loss_cl = model.cl_lossaug(output1, output2, node_mask, neg_mask, debias)
    else:
        pass
            
    if neg_type == 0:    
        if epoch<=50:  # 前50轮中，对比损失是依赖标签来计算的，但是此时的acc_train比较低。所以计算出来的对比损失意义不大，所以乘以0.0001来缩小
            loss = loss_train + 0.0001 * loss_cl
        else:
            loss = loss_train + 0.8 * loss_cl
        #loss = loss_train + 0.8 / (1 + math.exp(12 * (50.5 - epoch))) * loss_cl
    else:
        loss = loss_train + args.weight * loss_cl
    #loss = loss_train
    loss.backward()
    optimizer.step()
            
    if not args.fastmode:  # 不是快速模式，就重新用模型对图进行一次编码
        # Evaluate validation set performance separately,
        # deactivates dropout during validation run.
        model.eval()
        y_pre, _ = model(features, adj, encoder_type)

    loss_val = F.nll_loss(y_pre[idx_val], labels[idx_val])
    acc_val = accuracy(y_pre[idx_val], labels[idx_val])
    if acc_val > best_val_acc:
        best_val_acc = acc_val
        best_model = dcp(model)  # dcp保存在内存中，torch.save()保存到文件中，可以在其他环境中加载使用
            
    print('Epoch: {:04d}'.format(epoch+1),
          'loss_train: {:.4f}'.format(loss_train.item()),
          'loss_cl: {:.4f}'.format(loss_cl.item()),
          'acc_train: {:.4f}'.format(acc_train.item()),
          'loss_val: {:.4f}'.format(loss_val.item()),
          'acc_val: {:.4f}'.format(acc_val.item()),
          'time: {:.4f}s'.format(time.time() - t))


def test(model, features, adj, labels, idx_test, encoder_type):
    model.eval()
    y_pre, _ = model(features, adj, encoder_type)
    loss_test = F.nll_loss(y_pre[idx_test], labels[idx_test])
    acc_test = accuracy(y_pre[idx_test], labels[idx_test])
    print("Test set results:",
          "loss= {:.4f}".format(loss_test.item()),
          "accuracy= {:.4f}".format(acc_test.item()))
    return acc_test

######encoder is SGC, features update
def propagate1(feature, A, order, alpha):
    y = feature
    out = feature
    for i in range(order):
        y = torch.spmm(A, y).detach_()
        out = out + y
        
    return out.detach_()/(order + 1)
    
def propagate(feature, A, order, alpha):
    y = feature
    for i in range(order):
        y = (1 - alpha) * torch.spmm(A, y).detach_() + alpha * y
        
    return y.detach_()
    
def propagate2(features, adj, degree, alpha):
    ori_features = features
    emb = alpha * features
    for i in range(degree):
        features = torch.spmm(adj, features)
        emb = emb + (1-alpha)*features/degree
    return emb
    
if args.encoder == 'SGC':
    features = propagate(features, adj, 2, 0.)

#main 
if torch.cuda.is_available():
    features = features.cuda()
    adj = adj.cuda()
    labels = labels.cuda()
    idx_train = idx_train.cuda()
    idx_val = idx_val.cuda()
    idx_test = idx_test.cuda()
    data.edge_index = data.edge_index.cuda()



test_acc = torch.zeros(times)  # 保存test_acc结果
if torch.cuda.is_available():
    test_acc = test_acc.cuda()

seed = args.seed
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)

for i in range(times):
    best_model = None
    best_val_acc = 0.0
    # Model and optimizer
    if args.encoder == 'GCN':
        model = GCN(nfeat=features.shape[1],
                    nhid=args.hidden,
                    nclass=labels.max().item() + 1,
                    dropout=args.dropout,
                    tau = args.tau).cpu()
    else:
        model = MLP(nfeat=features.shape[1],
                    nhid=args.hidden,
                    nclass=labels.max().item() + 1,
                    dropout=args.dropout,
                    tau = args.tau).cuda()
    optimizer = optim.Adam(model.parameters(),
                lr=args.lr, weight_decay=args.weight_decay)
    
    # Train model
    t_total = time.time()
    print(args)
    for epoch in range(args.epochs):
        train(model, optimizer, epoch, features, adj, idx_train, idx_val, labels, args.data_aug, args.encoder_type, args.debias, args.kk, args.sample_size, args.neg_type)
    print("Optimization Finished!")
    print("Total time elapsed: {:.4f}s".format(time.time() - t_total))
    # Testing
    test_acc[i] = test(best_model, features, adj, labels, idx_test, args.encoder_type)


print("=== Final ===")
print("最高准确率:",torch.max(test_acc))
print("最低准确率:",torch.min(test_acc))
#print("30次平均",torch.mean(test_acc))
#print("30次标准差",test_acc.std())
#print("20次平均",torch.mean(test_acc[:20]))
#print("20次标准差",test_acc[:20].std())
print("10次平均",torch.mean(test_acc))
print("10次标准差",test_acc.std())
#import ipdb;ipdb.set_trace()

print(test_acc)
print(args)