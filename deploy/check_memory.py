"""Offline real-embedding smoke check. Uses fictional data and a temporary DB only."""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import types
from pathlib import Path


async def main():
    root = Path(sys.argv[1]).resolve()
    # Load as a tiny synthetic package: memory.py uses relative imports, while
    # this smoke check must not execute chat/__init__.py or connect Discord.
    package = types.ModuleType('_atri_memory_probe')
    package.__path__ = [str(root / 'chat')]
    sys.modules[package.__name__] = package
    spec = importlib.util.spec_from_file_location('_atri_memory_probe.memory', root / 'chat/memory.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    with tempfile.TemporaryDirectory(prefix='atri-memory-check-') as folder:
        service = module.MemoryService(Path(folder) / 'fictional.sqlite3')
        try:
            started = time.monotonic()
            scope = module.Scope('1', '10')
            args = dict(topic='饮食习惯', kind='preference',
                        content='我不吃辣，点菜时请选清淡的。', evidence='我不吃辣，点菜时请选清淡的。')
            await service.remember(scope, 0, author='55', message='100', source=args['evidence'], args=args)
            cold = time.monotonic() - started
            record = service.store.rows(scope)[0]
            assert record['vector'] and len(record['vector']) == 384 * 4, 'embedding fallback unexpectedly used'
            query = '这位用户在饮食方面有什么忌口？'
            started = time.monotonic()
            found, _ = await service.retrieve(scope, query)
            warm = time.monotonic() - started
            assert len(found) == 1 and found[0]['message'] == '100', 'semantic recall failed'
            for foreign in (module.Scope('1', '11'), module.Scope('2', '10')):
                rows, _ = await service.retrieve(foreign, query)
                assert not rows, 'scope boundary failed'
            social = dict(topic='深夜音乐分享', kind='impression', content='最近喜欢深夜分享音乐', evidence='今晚继续分享音乐')
            now = time.time()
            for index, when in enumerate((now - 8*3600, now)):
                message = str(200 + index)
                await service.remember(scope, 0, author='55', message=message, source=social['evidence'], args=social,
                                       sources=[dict(message=message, author='55', evidence=social['evidence'],
                                                     observed=when, support=True)])
                automatic, _ = await service.retrieve(scope, '晚上来点歌吧', author='55', automatic=True)
                assert any(r['kind'] == 'impression' for r in automatic) == bool(index), 'impression support gate failed'
            await service.remember(scope, 0, author='55', message='202', source='我想安静休息一阵',
                                   args=social | dict(content='最近希望安静休息', evidence='我想安静休息一阵', mode='replace'))
            automatic, _ = await service.retrieve(scope, '休息', author='55', automatic=True)
            assert not any(r['kind'] == 'impression' for r in automatic), 'correction inherited stale support'
            assert os.stat(service.store.path).st_mode & 0o777 == 0o600
            process = service.embedder.process
            service.store.manage(scope, action='clear')
            assert service.store.status(scope)['count'] == 0
            await service.close()
            assert process.returncode is not None
            print(json.dumps({'ok': True, 'dimensions': 384, 'cold_seconds': round(cold, 3),
                              'warm_seconds': round(warm, 3), 'offline': True,
                              'social_support_and_correction': True,
                              'cross_channel_blocked': True, 'cross_guild_blocked': True}))
        finally:
            await service.close()


if __name__ == '__main__':
    asyncio.run(main())
