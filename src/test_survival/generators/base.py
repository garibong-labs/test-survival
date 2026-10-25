from dataclasses import dataclass,field
from pathlib import Path
from typing import Protocol


@dataclass
class Funnel:
    files:int=0
    compiled:int=0
    kept:int=0


@dataclass
class RoundResult:
    files:list[Path]=field(default_factory=list)
    calls:list[dict]=field(default_factory=list)
    funnel:Funnel=field(default_factory=Funnel)


class Generator(Protocol):
    def generate(self,round_no:int)->RoundResult: ...
