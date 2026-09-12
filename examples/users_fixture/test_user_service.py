from src.users.service import UserService


def _service():
    return UserService(
        [
            {"id": 1, "email": "a@b.c"},
            {"id": 2, "email": "d@e.f"},
        ]
    )


def test_find_by_id_returns_user():
    assert _service().find_by_id(1)["email"] == "a@b.c"


def test_find_by_id_unknown_returns_none():
    assert _service().find_by_id(99) is None


def test_all_users_returns_copy():
    service = _service()
    users = service.all_users()
    assert len(users) == 2
