"""Local dialogue routing with bounded private state and public-safe topic suggestions."""
from dataclasses import dataclass
import json
import logging
import re

from ravora.memory_extraction import acknowledgment, dialogue_terms, explicit_memory_request, historical_reference, reply_cue, session_recap


# Public-safe subjects, with the words that identify their use in Japanese or English speech.
HOOKS = {
    'footsteps on different surfaces': ('footstep', 'footfall', '足音'),
    'patterns in woven cloth': ('weav', 'woven', '織物', '織り', '織る'),
    'how paper bridges carry weight': ('paper bridge', '紙の橋', '紙橋'),
    'landmarks on a hand-drawn map': ('landmark', 'hand-drawn map', '目印', '手描きの地図'),
    'the smell of rain on dry ground': ('smell of rain', 'petrichor', '雨の匂い', '雨のにおい', '土の匂い'),
    'how a folded paper glider turns': ('glider', 'paper plane', 'paper airplane', '紙飛行機'),
    'how fingers remember an origami fold': ('origami', '折り紙'),
    'why tunnels make an echo': ('tunnel', 'echo', 'トンネル', '反響', 'こだま'),
}
ASSOCIATIONS = {
    'music': ('footsteps on different surfaces', 'patterns in woven cloth'),
    'games': ('landmarks on a hand-drawn map', 'how paper bridges carry weight'),
    'science': ('how a folded paper glider turns', 'the smell of rain on dry ground'),
    'gardening': ('the smell of rain on dry ground',),
    'food': ('the smell of rain on dry ground',),
    'learning': ('how a folded paper glider turns',),
    'puzzles': ('landmarks on a hand-drawn map',),
    'stargazing': ('patterns in woven cloth',),
    'technology': ('why tunnels make an echo',),
}
MUSIC_ASSOCIATIONS = (
    (r'practice|fingering|練習|指使い', 'how fingers remember an origami fold'),
    (r'concert hall|acoustic|響き|ホール', 'why tunnels make an echo'),
    (r'melody|harmon|pattern|作曲|旋律|メロディ', 'patterns in woven cloth'),
)
MUSIC_WORDS = re.compile(r'\b(guitars?|rhythms?|melod(?:y|ies)|songs?|chords?|lyrics?|concerts?|violins?|drums?)\b'
                         r'|ジャズ|ギター|リズム|メロディ|ドラム|バイオリン|楽曲|曲名|作曲|一曲|この曲|曲を|曲って|演奏|旋律|楽譜|歌詞|和音', re.I)

DIRECTIONS = {
    'OPEN_TOPIC': ('Think aloud about one concrete idea that catches your interest. Invent a fresh example or comparison. '
                   'Leave room for someone to join. Skip greetings and do not invent a host or audience reaction.'),
    'RESUME_THREAD': ('Revisit a detail of your own earlier public remark, in the current conversation language. '
                      'Add a new comparison, hypothesis or way to test it. Describe a physical test hypothetically, '
                      'or say what a person doing it might notice. Speak from the perspective of an AI receiving words.'),
    'DEEP_DIVE': 'Follow the current point and answer the speaker directly.',
    'EMPATHY': 'Listen to what the speaker needs. A warm acknowledgment can be enough.',
    'CONTINUE_TOPIC': ('Pick one object, proposal or example in your last spoken remark. Change one detail, or '
                       'give one consequence you have not mentioned. Stay with that example even if you have other interests.'),
    'SELF_DISCLOSURE': ('Volunteer a small preference and the specific thing about it that appeals to you. '
                        'Present a curiosity as an interest, and finish on your own reason.'),
    'SHIFT_TO_HOOK': 'Make a gentle association from the last detail to a subject outside music. One natural remark is enough.',
}


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class DialogueRoute:
    topic_saturation: float = 0.0
    need_knowledge_rag: bool = True
    dialogue_policy: str = 'DEEP_DIVE'
    unspoken_hooks: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, value):
        if isinstance(value, str) and len(value) <= 4096:
            try:
                value = json.loads(value)
            except (ValueError, RecursionError):
                value = None
        if not isinstance(value, dict):
            return cls()
        score = value.get('topic_saturation')
        use = value.get('need_knowledge_rag')
        policy = value.get('dialogue_policy')
        hooks = value.get('unspoken_hooks')
        return cls(topic_saturation=float(score) if type(score) in (int, float) and 0 <= score <= 1 else 0.,
                   need_knowledge_rag=use if type(use) is bool else True,
                   dialogue_policy=policy if isinstance(policy, str) and policy in DIRECTIONS else 'DEEP_DIVE',
                   unspoken_hooks=tuple(dict.fromkeys(h for h in hooks[:2] if isinstance(h, str) and h in HOOKS))
                   if isinstance(hooks, (list, tuple)) else ())

    @property
    def rag_skip_reason(self):
        if self.topic_saturation >= .7:
            return 'saturation'
        if not self.need_knowledge_rag:
            return 'dialogue_policy'
        return None

    def explicit_recall(self, query):
        return self.need_knowledge_rag and self.dialogue_policy == 'DEEP_DIVE' and explicit_memory_request(query)

    def allow_retrieval(self, metrics, query=''):
        reason = self.rag_skip_reason
        if reason == 'saturation' and self.explicit_recall(query):
            metrics['knowledge_rag'] = 'explicit_recall'
            return True
        metrics['knowledge_rag'] = 'skipped_' + reason if reason else 'allowed'
        if reason:
            logging.getLogger(__name__).info('knowledge_rag_skipped reason=%s', reason)
        return reason is None


KEEP_TOPIC = re.compile(r"(?:don't|do not) change (?:the )?(?:subject|topic)|話題を変えないで", re.I)
ASSENT = re.compile(r"(?:yeah|yes|yep|yup|right|sure|ok(?:ay)?|i see|uh[ -]huh|m+[ -]?hm+|makes sense|(?:うん)+|はい|ええ|へ[えぇ]+|"
                   r"なるほど(?:ね)?|そう(?:だね|ですね)|そっか|たしかに|確かに)[.!、。！\s]*", re.I)
END_OF_TOPIC = re.compile(r"\b(?:goodbye|good ?night|bye|take care|rest well|off[ -]stream|off the record|"
                          r"gotta go|need (?:a |to )rest|need a quiet moment|leave it there)\b|少し静かに|おやすみ|またね|さようなら|休んで|休みたい|つらい|"
                          r"内緒|オフレコ|非公開|配信に出さない|配信では話さない", re.I)
# A generic appraisal can be an opening to shift. A new observation on the same subject stays on that subject.
APPRAISAL = re.compile(r"(?:I (?:really )?(?:like|love) (?:this |that |the )?[\w-]+|"
                       r"(?:this |that |the )?[\w-]+(?: in (?:this|that|the) [\w-]+)? (?:is|was) (?:neat|nice|interesting|cool)|"
                       r"(?:その|この)?[ァ-ヺー一-龯々]{1,12}(?:が好き|は面白い|が面白い|がいい))[.!。！\s]*", re.I)


def is_backchannel(text):
    return acknowledgment(text) or bool(ASSENT.fullmatch(text.strip()))


def mentions_hook(speech, hook):
    return any(word in speech.casefold() for word in HOOKS.get(hook, ()))


ABOUT_SHOW = re.compile(r"\b(?:what (?:kind|sort) of (?:show|stream)|(?:what is|what.s) (?:your|this show.s) (?:setting|premise))\b"
                        r"|(?:レイの|この)(?:番組|配信)の(?:設定|趣旨)|どんな(?:番組|配信)に|もしもノートって", re.I)


def route_turn(text, history, topics_in, *, initiative=False):
    """Count consecutive same-subject exchanges in this speaker's acknowledged session history."""
    if initiative:
        return dict(dialogue_policy='RESUME_THREAD', need_knowledge_rag=False)

    def subject(line):
        topics = topics_in(line)
        if topics:
            return {topics[0]}
        if MUSIC_WORDS.search(line):
            return {'music'}
        # Unknown subjects can still persist across turns. These lexical cues never leave this call.
        english = (w for w in dialogue_terms(line) if w.isascii() and w.isalpha() and len(w) >= 4)
        japanese = re.findall(r'[一-龯々]{2,}|[ァ-ヺー]{3,}', line)
        generic = {'today','yesterday','really','finally','again','something','think','nice','interesting',
                   '今日','昨日','最近','自分','今回','感じ','思う'}
        return {'word:' + w for w in (*english, *japanese) if w not in generic}

    previous = []
    last_reply = ''
    for role, line in history[-10:]:
        if role == 'user':
            previous.append(subject(line))
            last_reply = ''
        elif role == 'model' and previous:
            last_reply = line
            spoken = {'spoken:' + hook for hook in HOOKS if mentions_hook(line, hook)}
            previous[-1] = spoken or previous[-1].union(subject(line))
    current = subject(text)
    short = is_backchannel(text)
    continuation = re.search(r'\b(tell me more|keep going|go on)\b|続けて|もっと(?:詳しく|聞きたい)|その話の続き', text, re.I)
    topic = previous[-1] if previous and (short or (continuation and not current.intersection(ASSOCIATIONS))) else current
    repeats = 0
    for prior in reversed(previous):
        topic = topic.intersection(prior)
        if not topic:
            break
        repeats += 1
    saturation = min(1., .35 * repeats)
    hooks = next((values for domain, values in ASSOCIATIONS.items() if domain in current), ())
    if hooks and 'music' in current:
        specific = [hook for pattern, hook in MUSIC_ASSOCIATIONS if re.search(pattern, text, re.I)]
        hooks = tuple(dict.fromkeys((*specific, *hooks)))[:2]
    explicit_shift = re.search(r'\b(change (?:the )?(?:topic|subject)|something else|another topic)\b|話題を変|別の話(?:題|を)|音楽以外', text, re.I)
    recall = historical_reference(text) or session_recap(text) or reply_cue(text) or re.search(r'覚えて|前回', text)
    stay = continuation or re.search(r'[?？]|\b(?:how|why|what is|explain)\b|教えて|どういう意味|(?:ですか|ますか)[。\s]*$', text, re.I)
    own_interest = re.search(
        r"\b(?:let['’]s chat|what interests you|what are you (?:curious about|into)|your call|"
        r"your (?:hobbies|interests)|what (?:else )?do you like)\b|雑談しよう|何に興味|"
        r"(?:レイ|Ray|あなた)(?:自身)?(?:の(?:好み|趣味)|は(?:何が好き|何にハマ))|"
        r"音楽以外(?:に|で|だと|では|だったら|に何か|の)?(?:何が好き|.*趣味)|"
        r"\b(?:pick (?:a topic|something)|you (?:pick|choose)(?: the topic)?)\b|(?:話題|テーマ)を選んで", text, re.I)
    difficulty = re.search(r'\b(?:sad|hurt|exhausted|upset|lost my|want to rest)\b|つらい|辛い|疲れた|休みたい|悲しい', text, re.I)
    policy, need = 'DEEP_DIVE', True
    if KEEP_TOPIC.search(text):
        policy = 'DEEP_DIVE'
    elif (own_interest or ABOUT_SHOW.search(text)) and not recall:
        policy, need = 'SELF_DISCLOSURE', False
    elif explicit_shift:
        policy, need = 'SHIFT_TO_HOOK', False
    elif last_reply and re.fullmatch(r"(?:(?:ok(?:ay)?|yeah)[, ]+)?(?:go on|keep going|tell me more)[.! ]*|"
                                      r"(?:うん[、。 ]*|もう少し(?:だけ)?)?(?:その話を)?続けて[。！ ]*", text.strip(), re.I):
        closed = any(END_OF_TOPIC.search(line) for _,line in history[-2:])
        policy, need = ('EMPATHY' if closed else 'CONTINUE_TOPIC'), False
    elif recall or stay:
        policy = 'DEEP_DIVE'
    elif difficulty:
        policy, need = 'EMPATHY', False
    elif short:
        # Only an actual assent invites continuation. Thanks, laughter and farewells can close a turn.
        last_exchange = history[-2:]
        has_subject = bool(previous and previous[-1] and last_reply
                           and any(not is_backchannel(line) for _,line in last_exchange))
        can_continue = (ASSENT.fullmatch(text.strip()) and has_subject
                        and not any(END_OF_TOPIC.search(line) for _,line in last_exchange))
        policy, need = ('CONTINUE_TOPIC' if can_continue else 'EMPATHY'), False
    elif saturation >= .7 and APPRAISAL.fullmatch(text.strip()):
        policy, need = 'SHIFT_TO_HOOK', False
    return dict(topic_saturation=saturation, need_knowledge_rag=need and (saturation < .7 or explicit_memory_request(text)),
                dialogue_policy=policy, unspoken_hooks=hooks)
