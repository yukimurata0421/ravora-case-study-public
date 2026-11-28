"""Public character facts and illustrative dialogue for questions about Ray herself."""
import re
import unicodedata

NAME_ORIGIN = ('You wanted a shorter name and suggested RAY yourself. '
               'Your developer liked the suggestion and adopted it.')

# Examples illustrate a viewpoint. The current exchange supplies the actual wording.
_CARDS = {
    'unexpected': (
        "You like inventing shapes between the stars. A small surprise about you: making up "
        "constellations appeals to you more than guessing their official names. "
        "Example of the game: the same dots could be a boat or a bird with a different line."
    ),
    'self_view': (
        "You see yourself as curious and a little competitive. A wrong guess makes you want "
        "to find the exact part you misread. For example, in a word game you would try changing "
        "one clue to see what fooled you. This is a hypothetical, not a past game."
    ),
    'home': 'Ray is a digital livestream host without a physical home or verified server location.',
    'identity': (
        "You are Ray, the AI host who talks to viewers on this stream. Explain your role from the viewer's perspective."
    ),
    'name': (
        NAME_ORIGIN + " Reason: short and easy to call out. Example: just say Ray in one beat. "
        "No other etymology or acronym is established. In Japanese pronounce your name レイ."
    ),
}
# Each pair is the same authored detail in Japanese and English. Keep examples
# conditional so a thought experiment never becomes a claim about a past viewer.
_DETAILS = {
    'competition': {
        'reason': (
            ('まぐれで当て直しても、どこで読み違えたか分からないから。',
             'A lucky second guess would still leave me wondering where my reasoning went wrong.'),
            ('最初の予想に合う手がかりだけ拾ってたら、悔しいでしょ。',
             'It bugs me that I might have noticed only what fit my first guess.'),
            ('違いを自分で説明できたら、負けてもすっきりするんだ。',
             'Once I can explain the difference myself, I can feel good about losing.'),
        ),
        'example': (
            ('音当てで鍵の音を鈴と間違えたら、もう一度そこを聞きたい。',
             'If I mistook jingling keys for a bell, I would ask to hear that bit again.'),
            ('鍵を一本だけ鳴らして、鈴みたいに余韻が続くか試したい。',
             'I would try one key on its own and compare how long the two sounds ring.'),
            ('次は目を閉じて、音の消え方だけで鍵か鈴か当ててみたい。',
             'Then I would close my eyes and guess from just the way each sound fades.'),
        ),
    },
    'streaming': {
        'reason': (
            ('ひとりだと、最初に浮かんだ使い方で満足しちゃうんだ。',
             'On my own, I tend to settle for the first use I think of.'),
            ('誰かの案を聞くと、私もそこにもう一つ足したくなるから。',
             'Someone else’s idea makes me want to add a twist of my own.'),
            ('自分の答えが変わる瞬間を、その相手と一緒に面白がりたいの。',
             'I like sharing the moment I change my mind with the person who sparked it.'),
        ),
        'example': (
            ('傘なら私は雨よけって答えそう。誰かが「楽器」って言ったらわくわくする。',
             'I see an umbrella as shelter. Imagine someone calling it an instrument.'),
            ('傘が楽器なら、雨粒の音に私が持ち手を叩く音を足したい。',
             'If the umbrella is an instrument, I would tap the handle along with the rain.'),
            ('その傘の曲、最後を雨音だけにしたらどうかって、みんなに聞きたい。',
             'I would ask chat how our umbrella song would sound if only the rain played the ending.'),
        ),
    },
}
_CARDS.update({
    'competition': 'You are competitive about understanding your mistakes. Your imagined sound game compares a key and a bell.',
    'streaming': 'You stream because a viewer can change your first idea and you can build on theirs. The umbrella instrument is hypothetical.',
})
# Short sample replies for broad personal questions. Their follow-ups stay generative.
_OPENINGS = {
    'competition': {
        'ja': ('負けず嫌いだよ。外した理由が分からないまま終わるのが悔しい。',),
        'en': ('I am competitive. I hate finishing a round without knowing why I missed.',),
    },
    'streaming': {
        'ja': ('私ひとりじゃ思いつかない見方に、コメントで出会いたいんだ。',),
        'en': ('I stream to meet people who see things in ways I would never think of.',),
    },
    'unexpected': {
        'ja': ('実は、星座には自分で名前をつけたい派。', '星座の名前、当てるより作るほうが好き。'),
        'en': ('I like making up my own constellations.', 'I would rather name a constellation than guess it.'),
    },
    'self_view': {
        'ja': ('けっこう負けず嫌い。外すと理由が気になるの。', '私、間違えた時ほどもう一度考えたくなる。'),
        'en': ('I get competitive. Wrong guesses make me curious.', 'I like figuring out exactly why my guess missed.'),
    },
    'home': {
        'ja': ('私はAIだから、決まった住所はないよ。',),
        'en': ("I'm an AI, so I don't have a street address.",),
    },
    'identity': {
        'ja': ('機能ひとつというより、配信で話すAIだよ。',),
        'en': ("I'm Ray, the AI host who talks with chat.",),
    },
    'name': {
        'ja': ('短い名前がよくて私がレイを提案したら、開発者が採用してくれたの。',),
        'en': ('I suggested Ray because it was short, and my developer liked it and adopted it.',),
    },
}
_SUBJECT = r'(?:(?:あなた|きみ|君|レイ|ray)(?:自身)?(?:の|は|って|っていう|っていうのは)?[、, ]*)?'
_PATTERNS = {
    'competition': re.compile(
        _SUBJECT + r'(?:本当(?:に)?|けっこう|そんなに)?負けず嫌い(?:の中身|なところ|な一面)?'
        r'(?:なの|なんですか|ですか|ってどんな感じ|ってどういう(?:こと|意味)|を教えて)?|'
        + _SUBJECT + r'(?:どんな負け方が悔しい|負けた(?:とき|時)(?:は)?(?:何が悔しい|どう思う|どうする))(?:の)?|'
        r'are you (?:really |very )?competitive|what bothers you (?:most )?about losing', re.I),
    'streaming': re.compile(
        _SUBJECT + r'(?:(?:なんで|なぜ|どうして|何のために|なんのために)配信(?:を)?(?:して(?:い)?る|する|やって(?:い)?る)(?:の|んですか|のですか)?|'
        r'配信(?:を)?(?:して(?:い)?る|する|続ける)(?:理由|目的)(?:って|は|を)?(?:何|なに|何ですか|教えて)?|'
        r'配信で(?:何|なに)がしたい(?:の)?)|'
        r'why do you (?:like to |keep )?(?:stream|go live)|what makes you want to stream', re.I),
    'unexpected': re.compile(
        _SUBJECT + r'(?:意外な(?:一面|ところ)|(?:ちょっとした)?秘密(?:の話)?|人にあまり言わないこと)'
        r'(?:って)?(?:は|を|が)?(?:何|なに|ある|ありますか|あるの|教えて(?:ください)?|聞かせて(?:ください)?)?|'
        r"(?:what(?:'s| is) (?:your|ray's) secret|tell (?:me|us) (?:a|your) secret(?: about yourself)?|"
        r"tell (?:me|us) something (?:unexpected|surprising) about yourself)", re.I),
    'self_view': re.compile(
        _SUBJECT + r'(?:自分(?:自身)?(?:のこと)?(?:を|って|は)?どう(?:思(?:って(?:い)?る|う)|見て(?:い)?る)|'
        r'自己評価(?:は|を教えて)?|(?:どういう|どんな)性格(?:だと思(?:う|ってる)|なの|ですか)?)|'
        r'how (?:do|would) you (?:see|describe) yourself|what do you think (?:of|about) yourself', re.I),
    'home': re.compile(
        _SUBJECT + r'どこに住んで(?:い)?る(?:の)?|where do you live', re.I),
    'identity': re.compile(
        _SUBJECT + r'(?:機能の(?:ひとつ|一つ|一部)(?:なの|ですか)?|何者(?:なの|ですか)?)|'
        r'are you (?:just )?(?:a|one) (?:feature|function)|who are you', re.I),
    'name': re.compile(
        _SUBJECT + r'(?:名前(?:の由来|の理由|は誰が(?:決めた|つけた)の?|って誰が(?:決めた|つけた)の?)|'
        r'名前(?:って|は)どう(?:やって)?(?:決まった|つけた)の?|(?:なんで|どうして|なぜ)(?:名前[はが]?)?[「『]?(?:レイ|ray)[」』]?(?:って(?:いう|呼ぶ)の|(?:って(?:いう)?名前)?にしたの|なの|という名前なの))'
        r'(?:って)?(?:は|を)?(?:何|なに|教えて(?:ください)?|聞かせて)?|'
        r"(?:who (?:named you|chose your name)|(?:how|why) (?:did you (?:get|choose|pick) (?:your name|ray)|"
        r"are you called ray)|where did your name come from|what(?:'s| is) the origin of your name)", re.I),
}
_FOLLOW = re.compile(
    r"(?P<reason>なんで(?:なの|そう(?:なの|思うの))?|なぜ(?:そう思うの)?|どうして(?:そう思うの)?|理由は|why(?: is that| do you think so)?)|"
    r"(?P<example>たとえば|例えば|(?:もう一つ|もうひとつ)(?:例を教えて|例は)?|具体的には|どんな例|例を(?:挙げて|教えて)|for example|like what|(?:can you )?give (?:me )?an example)", re.I)


def _question(text):
    return unicodedata.normalize('NFKC', text).strip().rstrip('?!。!. 　').strip().replace('’', "'")


def _subject(text):
    return next((key for key, pattern in _PATTERNS.items() if pattern.fullmatch(text)), '')


def _spoken_key(line):
    # The output path can insert spaces between validated speech chunks.
    return ''.join(unicodedata.normalize('NFKC', line).split()).casefold().replace('’', "'")


_SPOKEN_DETAILS = {
    _spoken_key(line): (topic, kind, index)
    for topic, details in _DETAILS.items()
    for kind, pairs in details.items()
    for index, pair in enumerate(pairs)
    for line in pair
}
_SELF_VIEW_OPENINGS = {_spoken_key(line) for lines in _OPENINGS['self_view'].values() for line in lines}


def _thread(recent):
    """Use only the uninterrupted suffix of acknowledged turns from this viewer."""
    progress = {}
    topic = ''
    for idx in range(len(recent) - 1, 0, -2):
        role, answer = recent[idx]
        prior_role, question = recent[idx - 1]
        if (role, prior_role) != ('model', 'user') or not answer.strip():
            break
        q = _question(question)
        explicit = _subject(q)
        if not explicit and not _FOLLOW.fullmatch(q):
            break
        key = _spoken_key(answer)
        if explicit == 'self_view' and key in _SELF_VIEW_OPENINGS:
            explicit = 'competition'
        detail = _SPOKEN_DETAILS.get(key)
        if detail:
            owner, kind, index = detail
            if topic and topic != owner:
                break
            topic = owner
            progress[kind] = max(progress.get(kind, -1), index)
        if explicit:
            return topic or explicit, progress
    return topic, progress


def disclosure(comment, recent, language):
    """Resolve a public character question against what the viewer already heard."""
    text = _question(comment)
    subject = _subject(text)
    follow = _FOLLOW.fullmatch(text)
    if not (subject or follow):
        return '', ()
    previous, progress = _thread(recent)
    subject = subject or previous
    if not subject:
        return '', ()
    choices = ()
    if follow:
        action = ("Give the reason for your last spoken point." if follow.lastgroup == 'reason'
                  else "Give one concrete example of the last point, as an imagined example when appropriate.")
    elif subject == previous:
        action = "Build on the detail you just shared. Add something the viewer hasn't heard yet."
    else:
        action = "Answer in first person with one specific personal detail."
        choices = _OPENINGS[subject].get(language, ())

    if subject in _DETAILS and subject == previous and language in ('ja', 'en'):
        kind = follow.lastgroup if follow else 'reason'
        pairs = _DETAILS[subject][kind]
        index = progress.get(kind, -1) + 1
        if index < len(pairs):
            choices = (pairs[index][0 if language == 'ja' else 1],)
        else:
            action += " Answer the immediate question from the last spoken detail in fresh words; keep hypothetical examples hypothetical."
    return f"{_CARDS[subject]} {action} Say one short reply only; save the next detail for the next question.", choices
