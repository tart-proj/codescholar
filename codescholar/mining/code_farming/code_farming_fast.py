import os
import sys
import ast
import glob
import attrs

from tqdm import tqdm
from typing import Dict, List, Tuple, Set

from codescholar.utils.logs import logger
from codescholar.utils import multiprocess
from codescholar.mining.code_farming.code_farming import (grow_idiom,
                                                          subgraph_matches,
                                                          _mp_subgraph_matches,
                                                          build_dataset_lookup)

MAX_WORKERS = 10


@attrs.define(eq=False, repr=False)
class MinedIdiom:
    idiom: ast.AST
    start: int
    end: int


def get_single_nodes(
    dataset: List[ast.AST],
    dataset_lookup: List[Dict[str, List]],
    gamma: float
) -> Set[ast.AST]:
    """Get all unique and frequent ast.stmt nodes in ast.walk
    order (bfs) that are not import or function/class definitions.

    Args:
        dataset (List[ast.AST]): list of program ASTs
        dataset_lookup (List[Dict[str, List]]): maps of type(node):occurences
        gamma (float): min frequency for mined program nodes

    Returns:
        _type_: a set of unique and frequent ast.stmt nodes in the dataset
    """
    stmts = []

    candidates: List[Tuple(ast.AST, Dict[str, List])] = []
    candidate_loc: List[Tuple(int, int)] = []

    for prog in dataset:
        for i in ast.walk(prog):
            if(isinstance(i, ast.stmt)
                and not isinstance(i, (ast.FunctionDef,
                                       ast.AsyncFunctionDef,
                                       ast.AsyncFunctionDef,
                                       ast.ClassDef,
                                       ast.Import,
                                       ast.ImportFrom))):
                
                candidates.append((i, dataset_lookup))
                candidate_loc.append((i.lineno, i.end_lineno))

    subgraph_mp_iter = multiprocess.run_tasks_in_parallel_iter(
        _mp_subgraph_matches,
        tasks=candidates,
        use_progress_bar=False,
        num_workers=MAX_WORKERS)

    for c, loc, result in zip(candidates, candidate_loc, subgraph_mp_iter):

        if (
            result.is_success()
            and isinstance(result.result, int)
            and result.result >= gamma
        ):
            stmts.append(MinedIdiom(c[0], loc[0], loc[1]))
    
    return set(stmts)


def save_idiom(mined_results, candidate_idiom, loc, nodecount, fileid):
    new_idiom = MinedIdiom(candidate_idiom, loc[0], loc[1])
    
    if nodecount not in mined_results:
        mined_results[nodecount] = {}
        mined_results[nodecount][fileid] = [new_idiom]

    elif fileid not in mined_results[nodecount]:
        mined_results[nodecount][fileid] = [new_idiom]

    else:
        mined_results[nodecount][fileid].append(new_idiom)

    return mined_results


def _mp_code_miner(args):
    mined_results, node_count, fileid, dataset_lookup, gamma = args
    return filewise_code_miner(mined_results, node_count, fileid,
                               dataset_lookup, gamma)


def filewise_code_miner(mined_results, node_count,
                        fileid, dataset_lookup, gamma):

    for idiom in mined_results[node_count][fileid]:
        candidates: List[Tuple(ast.AST, Dict[str, List])] = []
        candidates_loc: List[Tuple(int, int)] = []

        # pass 1: create candidate idioms by combining w/ single nodes
        for prog in mined_results[1][fileid]:
            candidate_idiom = None

            # don't grow unnatural sequence of operations
            if prog.end <= idiom.end:
                continue

            try:
                candidate_idiom = grow_idiom(idiom.idiom, prog.idiom)
            except:
                continue
            finally:
                if candidate_idiom is not None:
                    candidates.append((candidate_idiom, dataset_lookup))
                    candidates_loc.append((idiom.start, prog.end))

        # pass 2: prune candidate idioms based on frequency
        for (c, dataset_lookup), loc in zip(candidates, candidates_loc):
            result = subgraph_matches(c, dataset_lookup)

            if result >= gamma**(1 / node_count):
                mined_results = save_idiom(mined_results,
                                           c, loc,
                                           node_count + 1,
                                           fileid)
            else:
                continue
    
    return mined_results


def codescholar_codefarmer(
    dataset: List[ast.AST],
    gamma: float,
    fix_max_len: bool = False,
    max_len: int = 0
) -> dict:

    dataset, dataset_lookup = build_dataset_lookup(dataset)
    gamma = gamma * len(dataset)
    node_count: int = 1

    mined_results: dict = {}
    mined_results[1] = {}

    print("==" * 20 + " [[CodeScholar::CodeFarmer Gen 0]] " + "==" * 20)
    for fileid, prog in enumerate(tqdm(dataset)):
        mined_results[1][fileid] = get_single_nodes([prog],
                                                    dataset_lookup,
                                                    gamma)
    
    while (node_count in mined_results):
        print("==" * 20 + f" [[CodeScholar::CodeFarmer Gen {node_count}]] " + "==" * 20)
        
        file_ids = mined_results[node_count].keys()

        if MAX_WORKERS > 1:

            # create parallel tasks
            codefarmer_tasks = [
                (mined_results, node_count, fileid, dataset_lookup, gamma)
                for fileid in file_ids
            ]

            # define a multiprocess worker
            miner_mp_iter = multiprocess.run_tasks_in_parallel_iter(
                _mp_code_miner,
                tasks=codefarmer_tasks,
                use_progress_bar=True,
                num_workers=MAX_WORKERS)

            for fileid, result in tqdm(zip(file_ids, miner_mp_iter),
                                       total=len(file_ids)):
                if (result.is_success() and isinstance(result.result, Dict)):
                    mined_results = result.result
        else:
            for fileid in file_ids:
                mined_results = filewise_code_miner(mined_results,
                                                    node_count, fileid,
                                                    dataset_lookup, gamma)

        # print the results:
        for fileid, g in mined_results[node_count].items():
            for p in g:
                print(ast.unparse(p.idiom))
                print("-" * 10 + "\n")

        node_count += 1

        if((fix_max_len and node_count > max_len)
                or (gamma**(1 / node_count) < 1)):
            break

    return mined_results


if __name__ == "__main__":
    dataset = []
    path = "../../data/Python-master"

    for filename in sorted(glob.glob(os.path.join(path, '*.py'))):
        with open(os.path.join(path, filename), 'r') as f:
            try:
                dataset.append(ast.parse(f.read()))
            except:
                pass
    
    mined_code = codescholar_codefarmer(dataset, gamma=0.4,
                                        fix_max_len=True, max_len=5)

    # ******************* CREATE MINING CAMPAIGN SUMMARY *******************

    print("==" * 20 + " [[CodeScholar::Concept Miner Summary]] " + "==" * 20)
    print(f"Dataset: {len(dataset)} progs")
    print(f"# Explorations: {len(mined_code)}")
    print("==" * 60)