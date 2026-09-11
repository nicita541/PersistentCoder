class ContractBuilder:
    def build(self, task):
        return {"success_criteria": list(task.success_criteria), "requires": list(task.requires), "produces": list(task.produces)}
