class EvidenceCollector:
    def collect(self,data):
        if isinstance(data,dict):
            return [f"{k}:{v}" for k,v in data.items() if v]
        return [str(data)]
