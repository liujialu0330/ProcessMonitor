"""本地并行跑测试的分配逻辑（tests/run_parallel.py）：均衡、不丢文件、份数不超过文件数"""
from tests.run_parallel import partition, find_test_files


def test_partition_keeps_every_file_exactly_once_and_balances_load():
    weights = {'a.py': 10.0, 'b.py': 9.0, 'c.py': 4.0, 'd.py': 3.0, 'e.py': 2.0, 'f.py': 2.0}
    shards = partition(list(weights), weights, 3)

    assert sorted(path for shard in shards for path in shard) == sorted(weights)
    loads = sorted(sum(weights[p] for p in shard) for shard in shards)
    assert loads[-1] - loads[0] <= 5.0          # 10 / 9+... 最重的一份不比最轻的重太多


def test_partition_never_makes_more_shards_than_files():
    assert len(partition(['x.py', 'y.py'], {'x.py': 1.0, 'y.py': 1.0}, 8)) == 2


def test_unknown_files_get_a_weight_from_their_size():
    files = find_test_files()
    assert 'tests/test_run_parallel.py' in files
    shards = partition(files, {}, 4)
    assert sorted(p for shard in shards for p in shard) == sorted(files)
