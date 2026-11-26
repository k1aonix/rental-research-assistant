from typing import Any, Literal
from pydantic import Field
from .schemas import DTO

class AssistantCommand(DTO):
    version: int = Field(ge=0)
    action: Literal['draft','generate','confirm','revise','end','check','retry']
    original_message: str = Field(default='', max_length=12000)
    goal: str = Field(default='', max_length=4000)
    style: Literal['柔和','审慎','坚定'] = '柔和'
    reply: str = Field(default='', max_length=12000)
    background: str = Field(default='', max_length=8000)

class ImageUpload(DTO):
    file_name: str = Field(min_length=1,max_length=180)
    content_base64: str = Field(max_length=14000000)

class Upload(ImageUpload):
    version: int = Field(ge=0)
    additional_images: list[ImageUpload] = Field(default_factory=list,max_length=19)

class CommunicationResult(DTO):
    reply: str = Field(min_length=1,max_length=12000,description='可复制发送的租客第一人称话术，直接回复房东或当前对方；不混入给用户的分析、提醒或代拟标签。')
    recognized_topic: str
    send_checks: list[str]
    signals: str
    remaining_questions: list[str]

class OCRResult(DTO):
    readable: bool
    text: str

class Clause(DTO):
    title: str
    quote: str
    location: str
    kind: Literal['existing','unclear','missing','background_conflict']
    reason: str
    question: str

class ContractResult(DTO):
    status: Literal['需协商','暂缓签约','未发现明确风险']
    summary: str
    recognized_summary: dict[str,str | None]
    sources: dict[str,str] = Field(description='字段名到逐字合同原文引用；值必须直接来自document，禁止添加document、页码、位置或解释前缀；缺失字段可省略')
    pre_signing_questions: list[str]
    clause_notices: list[Clause]
    disclaimer: str
