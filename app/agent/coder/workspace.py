from pathlib import Path

class Workspace:
    def __init__(self, root):
        self.root=Path(root)

    def exists(self,path):
        return (self.root/path).exists()

    def read(self,path):
        return (self.root/path).read_text(encoding="utf-8")

    def write(self,path,content):
        target=self.root/path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target
