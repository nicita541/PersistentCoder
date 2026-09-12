class UserService:
    """Existing service: public API must not change."""

    def __init__(self, users):
        self._users = list(users)

    def find_by_id(self, user_id):
        for user in self._users:
            if user["id"] == user_id:
                return user

        return None

    def all_users(self):
        return list(self._users)
