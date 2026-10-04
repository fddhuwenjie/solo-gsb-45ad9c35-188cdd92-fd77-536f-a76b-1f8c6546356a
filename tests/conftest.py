"""pytest 夹具：每个测试使用独立临时 SQLite 文件 + TestClient。"""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_file)

    from app import database as db_module
    from app.main import app

    # 预先建表；TestClient 启动时会以同一 DB_PATH 打开连接
    db_module.init_db(db_file)

    with TestClient(app) as c:
        yield c
