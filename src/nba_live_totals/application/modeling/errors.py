import re


class ModelingError(RuntimeError):
    def __init__(self, code: str) -> None:
        if type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
            raise ValueError("invalid modeling error code")
        self.code = code
        super().__init__(code)
