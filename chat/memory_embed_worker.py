"""Run only in the isolated fastembed environment; newline JSON stdin/stdout."""
import contextlib
import json
import sys

with contextlib.redirect_stdout(sys.stderr):
    from fastembed import TextEmbedding
    model = TextEmbedding(model_name=sys.argv[1], cache_dir=sys.argv[2], threads=2, local_files_only=True)

for line in sys.stdin:
    try:
        request = json.loads(line)
        text = request['text']
        if not isinstance(text, str) or len(text) > 2000:
            raise ValueError('invalid text')
        with contextlib.redirect_stdout(sys.stderr):
            vector = next(model.embed([text])).tolist()
        print(json.dumps({'vector': vector}), flush=True)
    except Exception:
        print(json.dumps({'error': 'embedding_failed'}), flush=True)
