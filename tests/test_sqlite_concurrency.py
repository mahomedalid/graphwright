import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize("operation", ["query", "tables", "columns", "foreign_keys", "close"])
def test_given_shared_connection_when_operations_overlap_then_query_times_out_without_deadlock(tmp_path, operation):
    child = textwrap.dedent("""
        import faulthandler
        import sqlite3
        import sys
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path

        from ddg.adapters.sqlite_adapter import SQLiteAdapter

        faulthandler.dump_traceback_later(5)
        path = Path(sys.argv[1])
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE numbers(n INTEGER)")
            connection.executemany("INSERT INTO numbers VALUES (?)", ((i,) for i in range(2000)))
        adapter = SQLiteAdapter("test", path, query_timeout_seconds=1)
        started = threading.Event()
        adapter._conn.set_trace_callback(lambda sql: started.set())
        operation = sys.argv[2]
        operations = {
            "query": lambda: adapter._execute("SELECT 1", 1),
            "tables": adapter._load_tables,
            "columns": lambda: adapter._load_columns("numbers"),
            "foreign_keys": lambda: adapter._load_foreign_keys("numbers"),
            "close": adapter.close,
        }
        with ThreadPoolExecutor(max_workers=2) as executor:
            expensive = executor.submit(
                adapter._execute,
                "SELECT sum(a.n * b.n * c.n) FROM numbers a, numbers b, numbers c", 1,
            )
            assert started.wait(2), "Expensive query did not start"
            other = executor.submit(operations[operation])
            try:
                expensive.result()
            except TimeoutError:
                pass
            else:
                raise AssertionError("Expensive query did not reach its timeout")
            result = other.result()
        if operation == "query":
            assert result[1] == [(1,)]
        elif operation == "tables":
            assert result == [{"name": "numbers", "type": "table", "comment": None}]
        elif operation == "columns":
            assert result[0]["name"] == "n"
        elif operation == "foreign_keys":
            assert result == []
        if operation != "close":
            assert adapter._execute("SELECT 1", 1)[1] == [(1,)]
            adapter.close()
        faulthandler.cancel_dump_traceback_later()
        print("completed")
    """)

    result = subprocess.run(
        [sys.executable, "-c", child, str(tmp_path / "concurrency.sqlite"), operation],
        capture_output=True, text=True, timeout=8,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "completed"
