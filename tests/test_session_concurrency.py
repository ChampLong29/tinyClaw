import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from tinyclaw.session.store import SessionStore


def test_session_index_remains_valid_under_cross_session_writes(tmp_path: Path):
    store = SessionStore(agent_id="main", base_dir=tmp_path / "sessions")
    session_ids = [store.create_session(label=f"session-{index}") for index in range(4)]

    def append_many(session_id: str) -> None:
        for index in range(25):
            store.append_transcript(
                session_id,
                {
                    "type": "user",
                    "content": f"{session_id}:{index}",
                    "ts": index,
                },
            )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append_many, session_ids))

    index = json.loads(store.index_path.read_text(encoding="utf-8"))
    assert set(index) == set(session_ids)
    assert all(index[session_id]["message_count"] == 25 for session_id in session_ids)
    for session_id in session_ids:
        assert len(store.load_session(session_id)) == 25
