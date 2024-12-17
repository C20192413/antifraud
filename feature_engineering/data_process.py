# %%
from collections import defaultdict
import pandas as pd
import numpy as np
from scipy.io import loadmat
import torch
import dgl
import random
import os
import time
import argparse
import pickle
import matplotlib.pyplot as plt
import networkx as nx
import scipy.sparse as sp
from sklearn.preprocessing import LabelEncoder

from tqdm import tqdm
from sklearn.preprocessing import StandardScaler
from argparse import ArgumentParser
# from . import *
DATADIR = os.path.join(os.path.dirname(
    os.path.abspath(__file__)), "..", "data/")


def featmap_gen(tmp_df=None):
    """
    Handle S-FFSD dataset and do some feature engineering
    :param tmp_df: the feature of input dataset
    """
    # time_span = [2, 5, 12, 20, 60, 120, 300, 600, 1500, 3600, 10800, 32400, 64800, 129600,
    #              259200]  # Increase in the number of time windows to increase the characteristics.
    time_span = [2, 3, 5, 15, 20, 50, 100, 150,
                 200, 300, 864, 2590, 5100, 10000, 24000]
    time_name = [str(i) for i in time_span]
    time_list = tmp_df['Time']
    post_fe = []
    for trans_idx, trans_feat in tqdm(tmp_df.iterrows()):
        new_df = pd.Series(trans_feat)
        temp_time = new_df.Time
        temp_amt = new_df.Amount
        for length, tname in zip(time_span, time_name):
            lowbound = (time_list >= temp_time - length)
            upbound = (time_list <= temp_time)
            correct_data = tmp_df[lowbound & upbound]
            new_df['trans_at_avg_{}'.format(tname)] = correct_data['Amount'].mean() # 平均值：窗口内的交易金额平均值。
            new_df['trans_at_totl_{}'.format(tname)] = correct_data['Amount'].sum() # 总和：窗口内的交易金额总和。
            new_df['trans_at_std_{}'.format(tname)] = correct_data['Amount'].std()  # 标准差：交易金额的波动性。
            new_df['trans_at_bias_{}'.format(tname)] = temp_amt - correct_data['Amount'].mean() # 均值和当前交易金额的差值。
            new_df['trans_at_num_{}'.format(tname)] = len(correct_data) # 交易次数：窗口内的交易次数。
            new_df['trans_target_num_{}'.format(tname)] = len(correct_data.Target.unique()) # 目标账户数：窗口内的目标账户数。
            new_df['trans_location_num_{}'.format(tname)] = len(correct_data.Location.unique()) # 交易地点数：窗口内的交易地点数。
            new_df['trans_type_num_{}'.format(tname)] = len(correct_data.Type.unique()) # 交易类型数：窗口内的交易类型数。
        post_fe.append(new_df)
    return pd.DataFrame(post_fe)


def sparse_to_adjlist(sp_matrix, filename):
    """
    Transfer sparse matrix to adjacency list
    :param sp_matrix: the sparse matrix
    :param filename: the filename of adjlist
    """
    # add self loop # 为稀疏矩阵添加自环
    homo_adj = sp_matrix + sp.eye(sp_matrix.shape[0]) 
    # create adj_list
    adj_lists = defaultdict(set)
    edges = homo_adj.nonzero() # 遍历稀疏矩阵的非零元素
    for index, node in enumerate(edges[0]): 
        adj_lists[node].add(edges[1][index]) # 加双向边
        adj_lists[edges[1][index]].add(node)
    with open(filename, 'wb') as file:
        pickle.dump(adj_lists, file)
    file.close()


def set_seed(seed):
    random.seed(seed)
    os.environ['PYTHONHASHSEED'] = str(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True


def MinMaxScaling(data):
    mind, maxd = data.min(), data.max()
    # return mind + (data - mind) / (maxd - mind)
    return (data - mind) / (maxd - mind)


def k_neighs( # (前驱/后继)k步邻居的节点索引(k=1/k=2)
    graph: dgl.DGLGraph,
    center_idx: int,
    k: int,
    where: str,
    choose_risk: bool = False,
    risk_label: int = 1
) -> torch.Tensor:
    """return indices of risk k-hop neighbors

    Args:
        graph (dgl.DGLGraph): dgl graph dataset
        center_idx (int): center node idx
        k (int): k-hop neighs
        where (str): {"predecessor", "successor"}
        risk_label (int, optional): value of fruad label. Defaults to 1.
    """
    target_idxs: torch.Tensor
    if k == 1:
        if where == "in":
            neigh_idxs = graph.predecessors(center_idx)
        elif where == "out":
            neigh_idxs = graph.successors(center_idx)

    elif k == 2:
        if where == "in":
            subg_in = dgl.khop_in_subgraph(
                graph, center_idx, 2, store_ids=True)[0]
            neigh_idxs = subg_in.ndata[dgl.NID][subg_in.ndata[dgl.NID] != center_idx]
            # delete center node itself
            neigh1s = graph.predecessors(center_idx)
            neigh_idxs = neigh_idxs[~torch.isin(neigh_idxs, neigh1s)]
        elif where == "out":
            subg_out = dgl.khop_out_subgraph(
                graph, center_idx, 2, store_ids=True)[0]
            neigh_idxs = subg_out.ndata[dgl.NID][subg_out.ndata[dgl.NID] != center_idx]
            neigh1s = graph.successors(center_idx)
            neigh_idxs = neigh_idxs[~torch.isin(neigh_idxs, neigh1s)]

    neigh_labels = graph.ndata['label'][neigh_idxs]
    if choose_risk:
        target_idxs = neigh_idxs[neigh_labels == risk_label]
    else:
        target_idxs = neigh_idxs

    return target_idxs


def count_risk_neighs(
    graph: dgl.DGLGraph,
    risk_label: int = 1
) -> torch.Tensor:

    ret = []
    for center_idx in graph.nodes():
        neigh_idxs = graph.successors(center_idx)
        neigh_labels = graph.ndata['label'][neigh_idxs]
        risk_neigh_num = (neigh_labels == risk_label).sum()
        ret.append(risk_neigh_num)

    return torch.Tensor(ret)


def feat_map():
    tensor_list = []
    feat_names = []
    for idx in tqdm(range(graph.num_nodes())):
        neighs_1_of_center = k_neighs(graph, idx, 1, "in")
        neighs_2_of_center = k_neighs(graph, idx, 2, "in")
        neighs_3_of_center = k_neighs(graph, idx, 2, "out")

        tensor = torch.FloatTensor([
            edge_feat[neighs_1_of_center, 0].sum().item(), # in_degree
            # edge_feat[neighs_1_of_center, 0].std().item(),
            edge_feat[neighs_2_of_center, 0].sum().item(), 
            # edge_feat[neighs_2_of_center, 0].std().item(),
            edge_feat[neighs_1_of_center, 1].sum().item(), # risk_num
            # edge_feat[neighs_1_of_center, 1].std().item(),
            edge_feat[neighs_2_of_center, 1].sum().item(),
            # edge_feat[neighs_2_of_center, 1].std().item(),
            edge_feat[neighs_3_of_center, 0].sum().item(), # in_degree
            edge_feat[neighs_3_of_center, 1].sum().item(), # risk_num
        ])
        tensor_list.append(tensor)

    feat_names = ["1hop_degree", "2hop_degree",
                  "1hop_riskstat", "2hop_riskstat", "3hop_degree", "3hop_riskstat"]
 
    tensor_list = torch.stack(tensor_list)
    return tensor_list, feat_names


if __name__ == "__main__":

    set_seed(42)

    parser = ArgumentParser()
    parser.add_argument("--origin", default=False)
    origin = vars(parser.parse_args())['origin']
    if origin:
        # %%
        """
            For Yelpchi dataset
            Code partially from https://github.com/YingtongDou/CARE-GNN
        """
        print(f"processing YELP data...")
        yelp = loadmat(os.path.join(DATADIR, 'YelpChi.mat')) # 读取邻接矩阵
        net_rur = yelp['net_rur']
        net_rtr = yelp['net_rtr']
        net_rsr = yelp['net_rsr']
        yelp_homo = yelp['homo']
        # 通过邻接矩阵生成邻接表
        sparse_to_adjlist(net_rur, os.path.join( 
            DATADIR, "yelp_rur_adjlists.pickle"))
        sparse_to_adjlist(net_rtr, os.path.join( 
            DATADIR, "yelp_rtr_adjlists.pickle"))
        sparse_to_adjlist(net_rsr, os.path.join(
            DATADIR, "yelp_rsr_adjlists.pickle"))
        sparse_to_adjlist(yelp_homo, os.path.join(
            DATADIR, "yelp_homo_adjlists.pickle"))

        data_file = yelp
        labels = pd.DataFrame(data_file['label'].flatten())[0]
        feat_data = pd.DataFrame(data_file['features'].todense().A)
        # load the preprocessed adj_lists
        with open(os.path.join(DATADIR, "yelp_homo_adjlists.pickle"), 'rb') as file:
            homo = pickle.load(file)
        file.close()
        src = []
        tgt = []
        for i in homo: # 整理邻接表中所有边
            for j in homo[i]: # i->j
                src.append(i)
                tgt.append(j)
        src = np.array(src)
        tgt = np.array(tgt)
        g = dgl.graph((src, tgt)) # 构造图
        g.ndata['label'] = torch.from_numpy(labels.to_numpy()).to(torch.long) # 为节点添加标签
        g.ndata['feat'] = torch.from_numpy(  # 为节点加特征
            feat_data.to_numpy()).to(torch.float32)
        dgl.data.utils.save_graphs(DATADIR + "graph-yelp.bin", [g]) # 保存图

        # %%
        """
            For Amazon dataset
        """
        print(f"processing AMAZON data...")
        amz = loadmat(os.path.join(DATADIR, 'Amazon.mat'))
        net_upu = amz['net_upu']
        net_usu = amz['net_usu']
        net_uvu = amz['net_uvu']
        amz_homo = amz['homo']

        sparse_to_adjlist(net_upu, os.path.join(
            DATADIR, "amz_upu_adjlists.pickle"))
        sparse_to_adjlist(net_usu, os.path.join(
            DATADIR, "amz_usu_adjlists.pickle"))
        sparse_to_adjlist(net_uvu, os.path.join(
            DATADIR, "amz_uvu_adjlists.pickle"))
        sparse_to_adjlist(amz_homo, os.path.join(
            DATADIR, "amz_homo_adjlists.pickle"))

        data_file = amz
        labels = pd.DataFrame(data_file['label'].flatten())[0]
        feat_data = pd.DataFrame(data_file['features'].todense().A)
        # load the preprocessed adj_lists
        with open(DATADIR + 'amz_homo_adjlists.pickle', 'rb') as file:
            homo = pickle.load(file)
        file.close()
        src = []
        tgt = []
        for i in homo:
            for j in homo[i]:
                src.append(i)
                tgt.append(j)
        src = np.array(src)
        tgt = np.array(tgt)
        g = dgl.graph((src, tgt))
        g.ndata['label'] = torch.from_numpy(labels.to_numpy()).to(torch.long)
        g.ndata['feat'] = torch.from_numpy(
            feat_data.to_numpy()).to(torch.float32)
        dgl.data.utils.save_graphs(DATADIR + "graph-amazon.bin", [g])

        # %%
        # """
        #     For S-FFSD dataset
        # """
        print(f"processing S-FFSD data...")
        data = pd.read_csv(os.path.join(DATADIR, 'S-FFSD.csv'))
        data = featmap_gen(data.reset_index(drop=True))
        data.replace(np.nan, 0, inplace=True)
        data.to_csv(os.path.join(DATADIR, 'S-FFSDneofull.csv'), index=None)
        data = pd.read_csv(os.path.join(DATADIR, 'S-FFSDneofull.csv'))

        data = data.reset_index(drop=True)
        out = []
        alls = []
        allt = []
        pair = ["Source", "Target", "Location", "Type"]
        for column in pair:
            src, tgt = [], []
            edge_per_trans = 3 # 相同组别中时间邻域大小(会向不早于当前时间最近的节点连边)
            for c_id, c_df in tqdm(data.groupby(column), desc=column):
                c_df = c_df.sort_values(by="Time") # 按时间排序
                df_len = len(c_df)
                sorted_idxs = c_df.index
                src.extend([sorted_idxs[i] for i in range(df_len) # 起点
                            for j in range(edge_per_trans) if i + j < df_len])
                tgt.extend([sorted_idxs[i+j] for i in range(df_len) # 终点
                            for j in range(edge_per_trans) if i + j < df_len])
            alls.extend(src)
            allt.extend(tgt)
        alls = np.array(alls)
        allt = np.array(allt)
        g = dgl.graph((alls, allt)) # 生成图
        cal_list = ["Source", "Target", "Location", "Type"]
        for col in cal_list:
            le = LabelEncoder()
            data[col] = le.fit_transform(data[col].apply(str).values)
        feat_data = data.drop("Labels", axis=1)
        labels = data["Labels"]
        g.ndata['label'] = torch.from_numpy( # 节点标签
            labels.to_numpy()).to(torch.long)
        g.ndata['feat'] = torch.from_numpy( # 节点特征
            feat_data.to_numpy()).to(torch.float32)
        dgl.data.utils.save_graphs(DATADIR + "graph-S-FFSD.bin", [g])
    # %%
    # generate neighbor riskstat features
    # k-hop代表从当前节点跳k次到达的节点，加前驱/后继标识跳的方向
    # 新增特征如下：
    # 节点u的入度
    # 1-hop后继节点中有欺诈行为的个数
    # 1-hop前驱节点的总度数
    # 1-hop前驱节点中中有欺诈行为的个数
    # 2-hop前驱节点的总度数
    # 2-hop前驱节点中有欺诈行为的个数
    for file_name in ['S-FFSD', 'yelp', 'amazon']:
        print(
            f"Generating neighbor risk-aware features for {file_name} dataset...")
        graph = dgl.load_graphs(DATADIR + "graph-" + file_name + ".bin")[0][0] # a.导入数据
        graph: dgl.DGLGraph
        print(f"graph info: {graph}")
        edge_feat: torch.Tensor
        degree_feat = graph.in_degrees().unsqueeze_(1).float() # b.统计节点入度
        risk_feat = count_risk_neighs(graph).unsqueeze_(1).float() # b.统计坏的节点个数

        origin_feat_name = []
        edge_feat = torch.cat([degree_feat, risk_feat], dim=1)
        origin_feat_name = ['degree', 'riskstat']

        features_neigh, feat_names = feat_map() # b.
        # ["1hop_degree", "2hop_degree", "1hop_riskstat", "2hop_riskstat"]
        # print(f"feature neigh: {features_neigh.shape}")

        features_neigh = torch.cat(
            (edge_feat, features_neigh), dim=1
        ).numpy()
        feat_names = origin_feat_name + feat_names
        features_neigh[np.isnan(features_neigh)] = 0. # c. 缺失值填充

        output_path = DATADIR + file_name + "_neigh_feat_modi.csv" # 
        features_neigh = pd.DataFrame(features_neigh, columns=feat_names)
        scaler = StandardScaler()
        # features_neigh = np.log(features_neigh + 1)
        features_neigh = pd.DataFrame(scaler.fit_transform( # d. 数据标准化
            features_neigh), columns=features_neigh.columns)

        features_neigh.to_csv(output_path, index=False) # e. 导出特征


