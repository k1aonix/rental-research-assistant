from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DTO(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Empty(DTO):
    pass


class Command(DTO):
    profile_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)


class SearchCommand(Command):
    preflight_token: str | None = None
    allow_partial: bool = False


class ConfirmTarget(Command):
    config_hash: str | None = None
    candidate_token: str | None = Field(default=None, max_length=10000)


class PreferenceInput(DTO):
    kind: Literal['agent', 'custom']
    original_text: str = Field(min_length=1, max_length=100)
    requirement: Literal['must', 'flexible']


class SaveProfile(Command):
    min_rent_cent: int = Field(ge=0)
    max_rent_cent: int = Field(ge=0)
    max_commute_min: int = Field(gt=0)
    max_walk_min: int = Field(ge=0)
    preferences: list[PreferenceInput]

    @model_validator(mode='after')
    def validate_profile(self):
        if self.min_rent_cent % 100 or self.max_rent_cent % 100:
            raise ValueError('预算请填写整数元')
        if self.min_rent_cent > self.max_rent_cent:
            raise ValueError('最低预算不能大于最高预算')
        if sum(item.kind == 'agent' for item in self.preferences) != 1:
            raise ValueError('必须有且仅有一个拒绝中介及二房东偏好')
        texts = []
        for item in self.preferences:
            if item.kind == 'agent' and item.original_text != '拒绝中介及二房东':
                raise ValueError('系统偏好文字不可修改')
            if item.kind == 'custom' and (not item.original_text.strip() or len(item.original_text) > 6):
                raise ValueError('自定义偏好最多6字')
            texts.append(item.original_text)
        if len(set(texts)) != len(texts):
            raise ValueError('偏好不可重复')
        return self


class ResetProfile(Command):
    old_profile_id: str
    confirm: Literal[True]


class ImportTask(Command):
    url: str = Field(min_length=1, max_length=3000)


class CancelTask(Command):
    confirm: Literal[True]


class Decision(Command):
    decision: Literal['continue', 'abort']


class Supplement(Command):
    name: str = Field(min_length=1, max_length=100)


class ResolveLocation(Command):
    poi_id: str | None = Field(default=None, min_length=1, max_length=100)
    community_name: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def one_location_action(self):
        if bool(self.poi_id)==bool(self.community_name) or self.community_name is not None and not self.community_name.strip():
            raise ValueError('请选择一个位置或填写修正的小区名称')
        return self


class RecordPreferenceInput(DTO):
    preference_id: str = Field(min_length=1, max_length=100)
    result: Literal['pass', 'fail', 'unknown']


class PatchRecord(Command):
    group: Literal['cost', 'commute', 'remark', 'all']
    rent_cent: int | None = Field(default=None, ge=0)
    monthly_cent: int | None = Field(default=None, ge=0)
    initial_cent: int | None = Field(default=None, ge=0)
    commute_sec: int | None = Field(default=None, ge=0)
    walk_sec: int | None = Field(default=None, ge=0)
    preference_results: list[RecordPreferenceInput] | None = None
    remark: str | None = Field(default=None, max_length=10000)

    @model_validator(mode='after')
    def group_fields(self):
        required = {'cost': {'rent_cent', 'monthly_cent', 'initial_cent'}, 'commute': {'commute_sec'}, 'remark': {'remark'},
                    'all': {'rent_cent', 'monthly_cent', 'initial_cent', 'commute_sec', 'remark'}}[self.group]
        supplied = self.model_fields_set - {'profile_id', 'version', 'group'}
        allowed = required | {'walk_sec', 'preference_results'} if self.group == 'all' else required
        if not required <= supplied <= allowed:
            raise ValueError('必须一次提交当前分组全部字段，不能跨组更新')
        if 'preference_results' in supplied and self.preference_results is None:
            raise ValueError('偏好结果不能为null')
        if self.group in ('remark', 'all') and self.remark is None:
            raise ValueError('备注用空字符串清空')
        return self


class ArchiveItem(DTO):
    record_id: str
    version: int = Field(ge=1)


class Archive(Command):
    items: list[ArchiveItem] = Field(min_length=1, max_length=100)
    confirm: Literal[True]


class ArchiveAll(Command):
    confirm: Literal[True]


class ArchiveAllView(DTO):
    archived_count: int


class SaveViewing(DTO):
    profile_id: str
    version: int = Field(ge=0)
    conclusion: Literal['undecided', 'proceed', 'hold', 'reject']
    summary: str = Field(max_length=10000)
    followup: str = Field(max_length=10000)
    general_checks: dict[str, bool]
    risk_checks: dict[str, bool]
    template_version: str


class Classification(DTO):
    type: Literal['rental', 'non_rental', 'unknown']
    reason: str


class Fact(DTO):
    text: str
    evidence_ids: list[str]


class BodyFacts(DTO):
    city: str | None
    rent_cent: int | None = Field(ge=0)
    community_name: str | None
    monthly_cent: int | None = Field(default=None, ge=0)
    initial_cent: int | None = Field(default=None, ge=0)
    facts: list[Fact]


class Investigation(DTO):
    facts: list[Fact]
    missing: list[str]
    conflicts: list[Fact]
    identity_hint: str | None = None


class PreferenceResult(DTO):
    preference_id: str
    result: Literal['pass', 'fail', 'unknown']
    reason: str
    evidence_ids: list[str]
    missing_source_kinds: list[str] = Field(default_factory=list)
    non_blocking_reason: str | None = None


class Risk(DTO):
    title: str
    reason: str
    evidence_ids: list[str]
    verification_hint: str


class ReportTopic(DTO):
    section: Literal['housing', 'preference', 'verification']
    preference_id: str | None = None
    topic: str = Field(min_length=1, max_length=60)
    conclusion: str = Field(min_length=1, max_length=1600)
    evidence_ids: list[str]
    status: Literal['supported', 'disputed', 'unknown']
    disagreements: list[Fact] = Field(default_factory=list, max_length=2)
    uncertainty: str | None = Field(default=None, max_length=600)


class Summary(DTO):
    report_topics: list[ReportTopic] = Field(default_factory=list, max_length=12)
    summary: str
    facts: list[Fact]
    preferences: list[PreferenceResult]
    conflicts: list[Fact]
    missing: list[str]
    risks: list[Risk]


MODEL_SCHEMAS = {'classification': Classification, 'body': BodyFacts, 'comments': Investigation,
                 'author': Investigation, 'community': Investigation, 'preference': Investigation, 'summary': Summary}


class PreferenceView(PreferenceInput):
    preference_id: str
    display_order: int


class ProfileView(DTO):
    schema_version: Literal[1]
    profile_id: str
    state: Literal['draft', 'locked']
    version: int
    min_rent_cent: int | None
    max_rent_cent: int | None
    max_commute_min: int | None
    max_walk_min: int | None
    target_snapshot: dict[str, Any]
    target_confirmed_at: str | None
    created_at: str
    preferences: list[PreferenceView]


class TaskView(DTO):
    policy_transition: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    search_preflight: dict[str, Any] | None = None
    record_results: list[dict[str, Any]] = Field(default_factory=list)
    legacy_policy: bool = False
    display_kind: str = 'current'
    stage_progress: list[dict[str, Any]] = Field(default_factory=list)
    can_skip: bool = False
    location_resolution: dict[str, Any] | None = None
    station_selection: dict[str, Any] | None = None
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    current_listing: dict[str, Any] | None = None
    step_states: dict[str, str] = Field(default_factory=dict)
    schema_version: Literal[1]
    task_id: str
    profile_id: str
    record_id: str | None
    mode: Literal['auto', 'manual', 'resume']
    state: Literal['queued', 'running', 'waiting_user', 'failed', 'completed', 'cancelled']
    version: int
    updated_at: str
    round: int | None
    current_step: str | None
    counters: dict[str, Any] | None
    pending_action: dict[str, Any] | None
    events: list[dict[str, Any]]
    error: dict[str, Any] | None
    planning_context: dict[str, Any] | None
    result: dict[str, Any]


class WorkspaceView(DTO):
    schema_version: Literal[1]
    profile: ProfileView | None
    fixed_config: dict[str, Any]
    search_defaults: dict[str, int]
    active_tasks: list[TaskView]
    recent_tasks: list[TaskView] = Field(default_factory=list)
    current_task_id: str | None = None
    csrf_token: str | None = None


class ManualValues(DTO):
    walk_sec: int | None = None
    rent_cent: int | None
    monthly_cent: int | None
    initial_cent: int | None
    commute_sec: int | None


class ConditionView(DTO):
    condition_id: str
    record_id: str
    condition_code: str
    preference_id: str | None
    result: Literal['pass', 'fail', 'unknown']
    decision: Literal['continue', 'abort'] | None
    version: int
    evidence_ids: list[str]
    route_context: dict[str, Any] | None


class RecordView(DTO):
    current_values: ManualValues | None = None
    value_sources: dict[str, str] = Field(default_factory=dict)
    body_evidence_ids: list[str] = Field(default_factory=list)
    evidence_sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    completeness_summary: dict[str, Any] | None = None
    resume_hint: str | None = None
    data_completeness: dict[str, Any] | None = None
    disposition: str = 'in_progress'
    preferences: list[dict[str, Any]] = Field(default_factory=list)
    screening_reason: str | None = None
    resume_stage: str | None = None
    schema_version: Literal[1]
    record_id: str
    profile_id: str
    list_status: Literal['candidate', 'screened', 'archived'] | None
    community_supplement: str | None
    remark: str
    created_at: str
    candidate_added_at: str | None
    version: int
    note_id: str
    source_url: str
    published_at: str | None
    title: str | None
    manual_values: ManualValues
    route_summary: dict[str, Any] | None
    full_report_id: str | None
    latest_brief_id: str | None
    risk_count: int
    risk_done: int
    viewing_conclusion: Literal['undecided', 'proceed', 'hold', 'reject']
    summary: str | None
    conditions: list[ConditionView]


class RecordPage(DTO):
    items: list[RecordView]
    next_cursor: str | None


class HistoryView(DTO):
    archived: RecordPage
    screened: RecordPage


class ReportView(DTO):
    report_sections: dict[str, Any] = Field(default_factory=dict)
    completeness_summary: dict[str, Any] | None = None
    evidence_sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    schema_version: Literal[1]
    report_id: str
    record_id: str
    kind: Literal['brief', 'full']
    generated_at: str
    content: dict[str, Any]


class EvidenceView(DTO):
    schema_version: Literal[1]
    evidence_id: str
    record_id: str
    excerpt: str
    uses: list[str]
    source: dict[str, Any]


class ViewingView(DTO):
    evidence_sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    schema_version: Literal[1]
    record_id: str
    version: int
    conclusion: Literal['undecided', 'proceed', 'hold', 'reject']
    summary: str
    followup: str
    saved_at: str | None
    general_checks: dict[str, bool]
    risk_checks: dict[str, bool]
    progress: dict[str, int]
    template: dict[str, Any]


class ExistingRecord(DTO):
    disposition: Literal['existing']
    record: RecordView


class DecisionView(DTO):
    condition: ConditionView
    task: TaskView


class SupplementView(DTO):
    record: RecordView
    task: TaskView


class ArchiveView(DTO):
    records: list[RecordView]


class SavedViewingView(DTO):
    viewing: ViewingView
    record: RecordView


class ErrorInfo(DTO):
    code: str
    message: str
    retryable: bool
    details: dict[str, Any]


class ErrorEnvelope(DTO):
    request_id: str
    error: ErrorInfo


def output_type(method, path):
    if '/assistants/' in path:
        return dict[str, Any]
    if path == '/api/v1/records/archive-all':
        return ArchiveAllView
    if path in ('/api/v1/target-candidates', '/api/v1/capabilities', '/capabilities', '/api/v1/search-preflight'):
        return dict[str, Any]
    if path in ('/api/v1/workspace', '/api/v1/profile/reset'):
        return WorkspaceView
    if path.startswith('/api/v1/profile/'):
        return ProfileView
    if path in ('/api/v1/tasks/import', '/api/v1/records/{id}/resume'):
        return TaskView | ExistingRecord
    if path.startswith('/api/v1/tasks/'):
        return TaskView
    if path.startswith('/api/v1/conditions/'):
        return DecisionView
    if path.endswith('/community'):
        return SupplementView
    if path == '/api/v1/records':
        return RecordPage
    if path == '/api/v1/history':
        return HistoryView
    if path.endswith('/viewing'):
        return ViewingView if method == 'GET' else SavedViewingView
    if path.startswith('/api/v1/reports/'):
        return ReportView
    if path.startswith('/api/v1/evidence/'):
        return EvidenceView
    if path == '/api/v1/records/archive':
        return ArchiveView
    return RecordView
