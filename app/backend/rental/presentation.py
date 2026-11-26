"""Human-readable quality summaries; raw diagnostic details stay in stored reports."""
import re
def quality_summary(data,notes=None):
    if not data and not notes:return None
    data=data or {}
    groups={};analysis_failed=False
    for item in data.get('sources',[]):
        kind=item.get('source_kind','')
        if kind.endswith('_analysis'):
            analysis_failed=analysis_failed or item.get('collection_status')=='failed'
            continue
        group=('作者资料' if kind.startswith('author') else '原帖评论' if kind.startswith(('original_comments','comments')) else '小区调查' if kind.startswith('community') else '偏好专项')
        groups.setdefault(group,set()).add(item.get('collection_status','partial'))
    available=[];limited=[];optional=[]
    impacts={'作者资料':'发布者身份和历史情况仍需核实。','原帖评论':'可能遗漏讨论或反例。','小区调查':'居住体验和小区情况仍需现场核实。','偏好专项':'偏好判断仅依据目前已有证据。'}
    for name,states in groups.items():
        if states=={'skipped_optional'}:optional.append(name+'未额外执行，综合判断使用已有资料。')
        elif states<= {'complete','empty_confirmed'}:available.append(name+('已确认暂无内容' if states=={'empty_confirmed'} else '已取得可用内容'))
        else:limited.append({'name':name,'status':'部分资料不足或未取得','impact':impacts[name]})
    reasons={x.get('reason') for x in data.get('sources',[])}
    clean=[]
    for text in notes or []:
        if text in reasons or any(k in text for k in ('not_collected','skipped_optional','辅助分析失败','AI_INVALID_')):continue
        text=re.sub(r'\b[a-f0-9]{24}\b|\b(?:evidence|record|step|pref)_[a-f0-9]+\b','',text).strip(' ：:；;')
        if text and text not in clean:clean.append(text)
    if analysis_failed:clean.append('部分辅助分析未完成，综合判断已使用现有原文继续分析。')
    return {'available':available,'limited':limited,'optional':optional,'notes':clean,'note':'以上是资料取得情况，不代表房源或所有偏好已被证实符合。'}


def report_sections(content):
    """Use S6 synthesis, never concatenate raw per-post investigation output on read."""
    sections = {'housing': [], 'preference': [], 'verification': [], 'legacy_summary': not bool(content.get('report_topics'))}
    seen = set()
    for topic in content.get('report_topics', []):
        section = topic.get('section')
        key = (section, topic.get('preference_id'), topic.get('topic'), topic.get('conclusion'))
        if section in ('housing', 'preference', 'verification') and key not in seen:
            seen.add(key)
            item = dict(topic)
            if item.get('status') == 'unknown' and '无法确认' not in item.get('conclusion', ''):
                item['conclusion'] = '无法确认。' + item['conclusion']
            sections[section].append(item)
    if not sections['housing']:
        # Existing reports already contain a final synthesis. No paid re-analysis on opening.
        text = content.get('summary') or '现有资料不足，房源整体情况无法确认，请现场核实。'
        sections['housing'].append({'topic': '房源概况', 'conclusion': text, 'evidence_ids': [], 'status': 'unknown', 'uncertainty': None, 'disagreements': []})
    if not content.get('report_topics') and content.get('conflicts'):
        # S6 conflicts are conclusions, not the source-by-source investigation records.
        conflicts = content['conflicts']
        texts = list(dict.fromkeys(x['text'] for x in conflicts if x.get('text')))
        ids = list(dict.fromkeys(eid for x in conflicts for eid in x.get('evidence_ids', [])))
        sections['housing'].append({'topic': '分歧与待核实',
            'conclusion': '综合分析提示：' + '；'.join(texts) + '。现有分歧尚未消解，无法确认实际情况。',
            'evidence_ids': ids, 'status': 'unknown', 'disagreements': [],
            'uncertainty': '请核实各说法是否针对同一房源及时间，不将其中一方自动视为已证实。'})
    return sections
