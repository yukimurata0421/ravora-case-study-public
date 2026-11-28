"""Rank approved viewer reports for a single fact or a two-topic comparison."""
import re
import unicodedata
from ravora.memory_extraction import ALIASES, dialogue_terms

CJK = re.compile(r'[一-龯々]{2,}|[ァ-ヺー]{2,}')


def normalize(text):
    return unicodedata.normalize('NFKC', text).casefold()


COMPARISON = re.compile(r'どっち|どちら|比べ|比較|何日(?:早|遅)|差は|\b(?:compare|earlier|later|difference between|how many days apart)\b', re.I)


NEAR = re.compile(r'さっき|たった今|直前|今(?:言|話)|\b(?:just (?:said|mentioned|told)|a moment ago|last message)\b', re.I)
OLDER = re.compile(r'先週|昨日|前回|先月|以前|前の配信|前に話|\b(?:yesterday|last (?:week|month|session|stream)|previous stream)\b', re.I)


def recent_reference(text):
    return bool(NEAR.search(text) and not OLDER.search(text))


def has_named_topics(text):
    # Short pronoun-only comparisons can use the exchanges already on air.
    return len(set(CJK.findall(text)))>=2 or len(set(dialogue_terms(normalize(text))))>=3


CLARIFICATIONS = {
    'recall-brand': {'ja': 'メーカーの名前、もう一度教えてくれる？', 'en': 'Could you remind me of the brand name?'},
    'recall-previous-day': {'ja': '前は何曜日だったか、もう一度教えてくれる？', 'en': 'Which day was it before the change?'},
    'recall-two-days': {'ja': '二つの予定、それぞれ何曜日だったか教えてくれる？', 'en': 'Which day of the week is each one?'},
    'recall-unknown': {'ja': '今は分からないよ。もう一度教えて？', 'en': "I can't tell from our chat. Could you remind me?"},
}


def clarification_kind(comment):
    """Ask for the missing detail using only the current comment and fixed phrases."""
    query = normalize(comment)
    if re.search(r'メーカー|ブランド|\b(?:brand|manufacturer)\b', query):
        return 'recall-brand'
    weekday = re.search(r'何曜|曜日|\b(?:weekday|day of the week)\b', query)
    if weekday and re.search(r'今まで|これまで|以前|変更前|前は|\b(?:before|previously|used to)\b', query):
        return 'recall-previous-day'
    if COMPARISON.search(query) and re.search(r'何日|曜日|同じ週|\b(?:days|week)\b', query):
        return 'recall-two-days'
    return 'recall-unknown'


def clarification_question(comment, language):
    return CLARIFICATIONS[clarification_kind(comment)][language]


def candidates(store, viewer, query, settings, turn, *, multiple=False):
    query = normalize(query)
    runs = CJK.findall(query)
    # Bigrams allow an approved tag to match inside a longer compound. FTS tokens stay quoted by search_memory.
    parts = [run[i:i+2] for run in runs for i in range(len(run)-1)]
    english = {ALIASES.get(w,w) for w in dialogue_terms(query) if w.isascii() and len(w)>=3}
    terms = list(dict.fromkeys([*runs, *parts, *sorted(english)]))[:16]
    if not terms:
        return []
    args = dict(scope=settings.mode, before_turn=turn, limit=10)
    matched = store.search_memory(viewer, ' '.join(terms), **args)
    if not matched:
        return []
    # A tie or a truncated pool cannot support a unique choice.
    if len(matched) == args['limit']:
        return []
    scores = []
    anchors = []
    for note in matched:
        tags = [normalize(tag) for tag in note['tags']]
        text = normalize(note['summary'])
        en_hits = english.intersection(dialogue_terms(text))
        tags_hit = {tag for tag in tags if len(tag)>=2 and (
            bool(re.search(r'(?<!\w)'+re.escape(tag)+r'(?!\w)',query)) if tag.isascii() else tag in query)}
        anchors.append(tags_hit)
        jp_hits = {p for p in parts if p in text}
        scores.append((len(tags_hit),len(en_hits)+len(jp_hits)))
    if multiple and COMPARISON.search(query):
        # Each report must have its own explicitly mentioned topic. Ambiguous third
        # matches and negation go through the ordinary single-report decision.
        hits = [(note,tags) for note,tags in zip(matched,anchors) if tags]
        if len(hits)==2:
            (a,left),(b,right)=hits
            if left-right and right-left and not re.search(r'じゃなく|ではなく|\b(?:except|instead of)\b',query):
                return [{k:n[k] for k in ('memory_id','summary','claim_kind')} for n in (a,b)]
    best = max(scores)
    if best == (0,0) or scores.count(best)!=1:
        return []
    item = matched[scores.index(best)]
    return [{k:item[k] for k in ('memory_id','summary','claim_kind')}]
