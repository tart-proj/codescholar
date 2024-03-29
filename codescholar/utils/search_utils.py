import os.path as osp
import numpy as np
import glob
import random
import re
from tqdm import tqdm
from typing import List
from itertools import chain
from multiprocessing import Pool

import torch
import networkx as nx
from elasticsearch import Elasticsearch
from deepsnap.graph import Graph as DSGraph


from codescholar.utils.train_utils import get_device
from codescholar.utils.graph_utils import GraphEdgeLabel, GraphNodeLabel


import redis

redis_client = redis.StrictRedis(host="localhost", port=6379, db=0)


########## SEARCH MACROS ##########


def _reduce(lists):
    """merge a nested list of lists into a single list"""
    return chain.from_iterable(lists)


def _frontier(graph, node, type="neigh"):
    """return the frontier of a node.
    The frontier of a node is the set of nodes that are one hop away from the node.

    Args:
        graph: the graph to find the frontier in
        node: the node to find the frontier of
        type: the type of frontier to find
            'neigh': the neighbors of the node (default) = out in a directed graph
            'radial': the union of the outgoing and incoming frontiers
    """

    if type == "neigh":
        return set(graph.neighbors(node))
    elif type == "radial":
        return set(graph.successors(node)) | set(graph.predecessors(node))


########## ELASTIC SEARCH UTILS ##########


def ping_elasticsearch():
    """check if elasticsearch is running"""
    es = Elasticsearch("http://localhost:9200/")
    try:
        info = es.info()
    except:
        return False

    return True


def ping_elasticindex(index_name: str = "python_files"):
    """check if elasticsearch index exists"""
    es = Elasticsearch("http://localhost:9200/")
    try:
        info = es.indices.get(index=index_name)
    except:
        return False

    return True


########## SEARCH REDIS UTILS ##########


def save_embeddings_to_redis(batch):
    pipeline = redis_client.pipeline()
    for key, value in batch:
        pipeline.set(key, value)
    pipeline.execute()


def load_embeddings(args, idx_list):
    embeddings = []
    for idx in idx_list:
        emb_path = osp.join(args.emb_dir, f"emb_{idx}.pt")
        embedding = torch.load(emb_path, map_location=torch.device("cpu"))
        emb_bytes = embedding.cpu().numpy().tobytes()
        embeddings.append((f"emb_{idx}", emb_bytes))
    return embeddings


def load_embeddings_batched_redis(args, prog_indices):
    batch_size = 100
    batches = [
        prog_indices[i : i + batch_size]
        for i in range(0, len(prog_indices), batch_size)
    ]

    with Pool() as pool:
        results = pool.starmap(load_embeddings, [(args, batch) for batch in batches])
        all_embeddings = [item for sublist in results for item in sublist]
        for batch in tqdm(
            [
                all_embeddings[i : i + batch_size]
                for i in range(0, len(all_embeddings), batch_size)
            ],
            desc="[redis_load]",
        ):
            save_embeddings_to_redis(batch)


def read_embeddings_batched_redis(args, prog_indices):
    embs = []

    for i in range(0, len(prog_indices), args.batch_size):
        batch_indices = prog_indices[i : i + args.batch_size]
        emb_keys = [f"emb_{idx}" for idx in batch_indices]
        emb_bytes_list = redis_client.mget(emb_keys)

        batch_embs = []
        for emb_bytes in emb_bytes_list:
            if emb_bytes:
                num_elements = len(emb_bytes) // 4
                assert num_elements % 64 == 0, "elements is not a multiple of 64"
                original_shape = (num_elements // 64, 64)
                emb_array = np.frombuffer(emb_bytes, dtype=np.float32).reshape(
                    original_shape
                )
                emb_tensor = torch.tensor(emb_array, dtype=torch.float32)
                batch_embs.append(emb_tensor)

        if batch_embs:
            embs.append(torch.cat(batch_embs, dim=0))

    return embs


def read_embeddings_redis(args, prog_indices):
    emb_keys = [f"emb_{idx}" for idx in prog_indices]
    emb_bytes_list = redis_client.mget(emb_keys)

    embs = []
    for emb_bytes in emb_bytes_list:
        if emb_bytes:
            num_elements = len(emb_bytes) // 4
            assert num_elements % 64 == 0, "elements is not a multiple of 64"
            original_shape = (num_elements // 64, 64)
            emb_array = np.frombuffer(emb_bytes, dtype=np.float32).reshape(
                original_shape
            )
            emb_tensor = torch.tensor(emb_array, dtype=torch.float32)
            embs.append(emb_tensor)

    return embs


########## SEARCH DISK UTILS ##########


def sample_programs(src_dir: str, k=10000, seed=24):
    np.random.seed(seed)
    files = [f for f in sorted(glob.glob(osp.join(src_dir, "*.pt")))]
    random_files = np.random.choice(files, min(len(files), k))
    random_index = [f.split("_")[-1][:-3] for f in random_files]

    return random_files, random_index


def graphs_from_embs(graph_dir, paths: List[str]) -> List:
    graphs = []
    for file in paths:
        graph_path = "data_" + file.split("_")[-1]
        graph_path = osp.join(graph_dir, graph_path)

        graphs.append(torch.load(graph_path, map_location=torch.device("cpu")))

    return graphs


# @cached(cache=LRUCache(maxsize=1000), key=lambda args, idx: hashkey(idx))
def read_graph(args, idx):
    graph_path = f"data_{idx}.pt"
    graph_path = osp.join(args.source_dir, graph_path)
    return torch.load(graph_path, map_location=torch.device("cpu"))


def read_prog(args, idx):
    prog_path = f"example_{idx}.py"
    prog_path = osp.join(args.prog_dir, prog_path)
    with open(prog_path, "r") as f:
        return f.read()


def read_embedding(args, idx):
    emb_path = f"emb_{idx}.pt"
    emb_path = osp.join(args.emb_dir, emb_path)
    return torch.load(emb_path, map_location=torch.device("cpu"))


def read_embeddings(args, prog_indices):
    embs = []
    for idx in prog_indices:
        embs.append(read_embedding(args, idx))

    return embs


def read_embeddings_batched(args, prog_indices):
    embs, batch_embs = [], []
    count = 0

    for i, idx in enumerate(prog_indices):
        batch_embs.append(read_embedding(args, idx))

        if i > 0 and i % args.batch_size == 0:
            embs.append(torch.cat(batch_embs, dim=0))
            count += len(batch_embs)
            batch_embs = []

    # add remaining embs as a batch
    if len(batch_embs) > 0:
        embs.append(torch.cat(batch_embs, dim=0))
        count += len(batch_embs)

    assert count == len(prog_indices)

    return embs


########## GRAPH HASH UTILS ##########

cached_masks = None


def vec_hash(v):
    global cached_masks
    if cached_masks is None:
        random.seed(2019)
        cached_masks = [random.getrandbits(32) for i in range(len(v))]

    v = [hash(v[i]) ^ mask for i, mask in enumerate(cached_masks)]
    return v


def wl_hash(g, dim=64):
    """weisfeiler lehman graph hash"""
    g = nx.convert_node_labels_to_integers(g)
    vecs = np.zeros((len(g), dim), dtype=int)

    for v in g.nodes:
        if g.nodes[v]["anchor"] == 1:
            vecs[v] = 1
            break

    for i in range(len(g)):
        newvecs = np.zeros((len(g), dim), dtype=int)
        for n in g.nodes:
            newvecs[n] = vec_hash(np.sum(vecs[list(g.neighbors(n)) + [n]], axis=0))
        vecs = newvecs

    return tuple(np.sum(vecs, axis=0))


######## IDIOM MINE UTILS ##########


def save_idiom(path, idiom):
    try:
        idiom = black.format_str(idiom, mode=black.FileMode())
    except:
        pass

    with open(path, "w") as fp:
        fp.write(idiom)


def _print_mine_logs(mine_summary):
    print("========== CODESCHOLAR MINE ==========")
    print(".")
    for size, hashed_idioms in mine_summary.items():
        print(f"├── size {size}")
        fin_idx = len(hashed_idioms.keys()) - 1

        for idx, (hash_id, count) in enumerate(hashed_idioms.items()):
            if idx == fin_idx:
                print(f"    └── [{idx}] {count} idiom(s)")
            else:
                print(f"    ├── [{idx}] {count} idiom(s)")
    print("==========+================+==========")


def _write_mine_logs(mine_summary, filepath):
    with open(filepath, "w") as fp:
        fp.write("========== CODESCHOLAR MINE ==========" + "\n")
        fp.write("." + "\n")
        for size, hashed_idioms in mine_summary.items():
            fp.write(f"├── size {size}" + "\n")
            fin_idx = len(hashed_idioms.keys()) - 1

            for idx, (hash_id, count) in enumerate(hashed_idioms.items()):
                if idx == fin_idx:
                    fp.write(f"    └── [{idx}] {count} idiom(s)" + "\n")
                else:
                    fp.write(f"    ├── [{idx}] {count} idiom(s)" + "\n")
        fp.write("==========+================+==========" + "\n")


############# FEATURIZER UTILS #############


def featurize_graph(g, feat_tokenizer, feat_model, anchor=None, device_id=None):
    assert len(g.nodes) > 0
    assert len(g.edges) > 0

    if anchor is not None:
        pagerank = nx.pagerank(g)
        clustering_coeff = nx.clustering(g)

        # Batch tokenization and embedding
        spans = [g.nodes[v]["span"] for v in g.nodes]
        spans = [re.sub("\s+", " ", span) for span in spans]
        tokens_ids = feat_tokenizer(
            spans, padding=True, truncation=True, return_tensors="pt"
        )
        tokens_tensor = tokens_ids["input_ids"].to(get_device(device_id))

        with torch.no_grad():
            context_embeddings = feat_model(tokens_tensor)[0]
        context_embeddings = torch.mean(context_embeddings, dim=1)

        # Assign features to nodes
        for i, v in enumerate(g.nodes):
            g.nodes[v]["node_feature"] = torch.tensor(
                [float(v == anchor)], dtype=torch.float, device=get_device(device_id)
            )

            node_type_name = g.nodes[v]["ast_type"]
            if isinstance(node_type_name, str):
                try:
                    node_type_val = GraphNodeLabel[node_type_name].value
                except KeyError:
                    node_type_val = GraphNodeLabel["Other"].value

                g.nodes[v]["ast_type"] = torch.tensor(
                    [node_type_val], device=get_device(device_id)
                )

            g.nodes[v]["node_span"] = context_embeddings[i].unsqueeze(0)
            g.nodes[v]["node_degree"] = torch.tensor(
                [g.degree(v)], dtype=torch.float, device=get_device(device_id)
            )
            g.nodes[v]["node_pagerank"] = torch.tensor(
                [pagerank[v]], dtype=torch.float, device=get_device(device_id)
            )
            g.nodes[v]["node_cc"] = torch.tensor(
                [clustering_coeff[v]], dtype=torch.float, device=get_device(device_id)
            )

    for e in g.edges:
        edge_type_name = g.edges[e]["flow_type"]

        if isinstance(edge_type_name, str):
            edge_type_val = GraphEdgeLabel[edge_type_name].value
            g.edges[e]["flow_type"] = torch.tensor([edge_type_val])

    # Note: no need to sort the nodes of the graph
    # to maintain an order. GNN is permutation invariant.

    return DSGraph(g)
