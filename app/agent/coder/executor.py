import subprocess

class CodeExecutor:
    def __init__(self, context=None, tools=None):
        self.context=context
        self.tools=tools or []

    def execute(self, task):
        commands=getattr(task, "commands", None) or []
        outputs=[]
        for command in commands:
            result=subprocess.run(command, shell=True, capture_output=True, text=True)
            outputs.append({"command":command,"code":result.returncode,"stdout":result.stdout,"stderr":result.stderr})
            if result.returncode:
                return {"ok":False,"outputs":outputs}
        return {"ok":True,"outputs":outputs,"task":str(task)}
