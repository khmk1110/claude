import pytest

from payroll import create_app, db


@pytest.fixture
def app(tmp_path):
    return create_app({"TESTING": True, "DATABASE": str(tmp_path / "t.sqlite3"), "SECRET_KEY": "t"})


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def conn(app):
    c = db.connect(app.config["DATABASE"])
    yield c
    c.close()
