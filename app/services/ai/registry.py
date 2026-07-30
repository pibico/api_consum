"""Skill registry — resolve domain skills by name.

Contracts today; invoices join the same table when their phase lands.
"""
from __future__ import annotations

from typing import Dict, Optional

from app.services.ai.advice import AdviceSkill
from app.services.ai.base import Skill
from app.services.ai.contracts import ContractSkill
from app.services.ai.invoices import InvoiceSkill

SKILLS: Dict[str, Skill] = {
    "contract": ContractSkill(),
    "invoice": InvoiceSkill(),
    "advice": AdviceSkill(),
}


def get(name: str) -> Optional[Skill]:
    return SKILLS.get(name)
