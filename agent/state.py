from dataclasses import dataclass,field

@dataclass
class AgentState:
    goal:str=""
    current_state:int=0
    plan:list=field(default_factory=list)
    history:list=field(default_factory=list)
    finished:bool=False