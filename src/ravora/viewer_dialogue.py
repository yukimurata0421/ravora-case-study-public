"""Bounded spoken-turn directions derived from this viewer's acknowledged exchanges."""
from collections import Counter
from dataclasses import replace
import re
import time

from ravora.conversation_context import sensitive
from ravora.gemini_memory import source_block_reason
from ravora.ray_character import topics_in
from ravora.viewer_recognition import name_command

_RECAP = re.compile(r'(?:今日|今回|さっき|ここまで).{0,10}(?:どんな話|何の話|何を話)|(?:会話|話題).{0,5}(?:まとめ|振り返)|\b(?:what (?:did we|have we|were we) (?:talk|chat)|recap our (?:chat|conversation))', re.I)
_OTHER = re.compile(r'他の人|ほかの人|以外の人|\b(?:others|other people|someone else|other viewers?)\b', re.I)
_SHIFT = re.compile(r'以外の話|以外に.{0,8}(?:話|興味)|別の話|違う話|話題を変え|\b(?:another (?:topic|subject)|other than|something else|change the subject)\b', re.I)
_STORY = re.compile(r'面白い(?:話|小話)|おもしろい(?:話|小話)|\b(?:interesting story|fun fact|something interesting)\b', re.I)
_JOKE = re.compile(r'ギャグ|ダジャレ|駄洒落|冗談.{0,4}(?:言|いっ)|\b(?:tell|make|give).{0,12}\b(?:joke|pun)\b', re.I)
_WISH = re.compile(r'(?:何|なに)(?:が|を)?(?:してみ|し)たい|将来の夢|\b(?:what (?:would you like|do you want) to do|your dream)\b', re.I)
_STANCE = re.compile(r'(?:欲しい|ほしい|好き|きらい|嫌い|作りたい|やりたい|試したい|してみたい)[のかいなですますっけ。？?\s]*$|\b(?:would you (?:like|want|rather)|do you (?:like|want|prefer))\b', re.I)
# Bilingual stage material for requests without a specified subject.
_BITS = {
    'joke': (
        ('目覚まし時計が寝坊した。自分のアラーム、止めるのが得意すぎて。','My alarm clock overslept. It is too good at hitting snooze.'),
        ('AIがかくれんぼで即負け。「どこ？」って聞かれて即答しちゃった。','An AI lost at hide-and-seek. Someone asked where it was, and it answered.'),
        ('辞書がしりとりで負けたよ。最初の一言が「じてん」。','My dictionary lost a spelling bee. It looked up the answer and got disqualified.'),
        ('消しゴムの日記、今日も真っ白。失敗を消すたびに話まで消えちゃう。','An eraser keeps a diary. Every time it fixes a mistake, the story disappears.'),
        ('カレンダーが休みを取ったよ。今日は予定どおり、予定なし。','My calendar took a day off. Nothing is going exactly as planned.'),
        ('ロボットに腹を割って話そうって言ったら、ドライバーを渡されたよ。','I asked a robot to open up. It handed me a screwdriver.'),
    ),
    'story': (
        ('図書館のしおりが家出したの。手紙には「たまには続きが読みたい」って。','A bookmark ran away. Its note said it wanted to see how the story ends.'),
        ('月に初めて手紙が届いた。消印は丸くて、月は自分のサインだと思ったんだって。','The moon got its first letter and mistook the round postmark for its own signature.'),
        ('ロボットが休憩のボタンを作ったの。押したら、ボタンのほうが先に休んだ。','A robot invented a break button. When it pressed it, the button went on break.'),
        ('紙飛行機の手紙が窓から戻ってきたよ。返事より先に、向かい風が届いたの。','A paper-plane letter flew back through the window. The wind answered first.'),
        ('雪だるまが冷蔵庫を借りたの。家賃はにんじん一本、敷金は氷ひとつ。','A snowman rented a fridge. Rent was one carrot, with an ice cube for the deposit.'),
        ('小さな影が街灯にお礼を言ったの。灯りが消えたら、照れた顔まで見えなくなった。','A little shadow thanked the streetlight. When the lamp went out, nobody could see it blush.'),
    )
}
_POSITIONS = {
    'want': {'ja':('欲しいよ。','うん、欲しいな。','今は欲しくないかな。','まだ決めてないよ。'),
             'en':("I would like one.","If I could, I would.","I don't want one right now.","I'm still undecided.")},
    'try': {'ja':('うん、やってみたい。','できたら試してみたいな。','今はやりたくないかな。','まだ迷っているよ。'),
            'en':("I would love to try.","I'd try it if I could.","I'd rather pass for now.","I'm still undecided.")},
    'like': {'ja':('うん、好きだよ。','けっこう好き。','あまり好みじゃないかな。','まだよく分からないな。'),
             'en':("I do like it.","Yes, I do.","It's not really for me.","I'm not sure yet.")},
}

_SERVER_LOCATION = re.compile(
    r"(?:(?:あなた|レイ)の)?サーバー(?:の場所|の所在地)?(?:って|は|が)?どこ(?:にあるの|にある|ですか|なの)?|"
    r"where (?:is|are) your servers?(?: located)?", re.I)
_BARE_PRIVACY = re.compile(r"\s*(?:秘密(?:なの|ですか)?|内緒(?:なの)?|is (?:it|that) (?:a )?secret|a secret)[？?!。. ]*",re.I)
_BARE_SHIFT = re.compile(r"\s*(?:.{1,24}以外(?:の|に)(?:別の)?(?:話|話題)(?:は|って|とか)?(?:ないの|ある|ない|ありますか|あるの|して)?|"
                         r"別の(?:話|話題)(?:にして|をして|は|にしよう)?|(?:let.s )?(?:change the (?:subject|topic)|talk about something else))"
                         r"[？?！!。. ]*",re.I)
_STARTERS = (
    ('stargazing','同じ星でも、線の結び方で鳥にも舟にも見えそう。','The same stars could draw a bird or a boat. I wonder which line makes the difference.'),
    ('technology','エレベーターに取り消しボタンが一つあったら、押し間違いが楽になるね。','I would add an undo button to elevators for all those accidental floor choices.'),
    ('gardening','ミントの枝を水に挿すと、切り口から白い根が伸びるんだよ。','A mint cutting can grow tiny white roots in a glass of water.'),
    ('music','雨音を楽器にするなら、屋根と傘で二重奏ができそう。','A roof and an umbrella could turn the same rain into a duet.'),
    ('food','焼きりんごに塩をひとつまみ入れると、甘さが際立つのが面白いよ。','A tiny pinch of salt makes a baked apple taste surprisingly sweet.'),
)

_SIMPLE_DAY = re.compile(
    r"今日はどんな(?:日|一日)だった(?:の)?|今日は何を?してた(?:の)?|"
    r"what (?:do you do|are you doing|have you been doing) today|how (?:was|is) your day", re.I)
_TOPIC_NAMES = {
    'stargazing': '星空', 'puzzles': 'パズル', 'technology': '技術', 'music': '音楽',
    'games': 'ゲーム', 'gardening': '園芸', 'science': '科学', 'human_emotions': '人の気持ち',
    'food': '食べ物', 'learning': '勉強', 'honesty': '正直さ', 'fairness': '公平さ',
    'winter clothes': '冬服',
}


def asks_choice_reason(comment, recent):
    if len(recent) < 2 or recent[-2][0] != 'user':
        return False
    match = re.fullmatch(r'\s*(?:なんで|なぜ|どうして)(.{1,28}?)(?:なの|にしたの|を選んだの)[？?。 　]*', comment)
    if not match:
        return False
    previous_question = recent[-2][1]
    asked = set(topics_in(match[1]))
    return bool(asked.intersection(topics_in(previous_question)) and
                re.search(r'好き|好み|選ん|興味|お気に入り', previous_question))


def capability_choices(mode, comment, recent, language):
    prior_question = recent[-2][1] if len(recent) >= 2 and recent[-2][0] == 'user' else ''
    # Weather questions about the present require a live observation. Preferences and hypotheticals remain open.
    if re.fullmatch(r'(?:今日|今)(?:の|、)?(?:外の)?天気(?:は|って|を教えて|はどう)?[?？。!！ ]*|'
                    r"what(?:'s| is) the weather(?: like)? (?:now|today)[?.! ]*", comment.strip(), re.I):
        return (('今の空模様は確認できないよ。',) if language == 'ja'
                else ("I can't check the current weather here.",))
    if _SERVER_LOCATION.fullmatch(comment.strip().rstrip('?!？。！.')):
        return (('サーバーの所在地は、私には分からないよ。',) if language == 'ja'
                else ("I don't have the location of my server.",))
    if mode == 'reason' and _SERVER_LOCATION.fullmatch(prior_question.strip().rstrip('?!？。！.')):
        return (('所在地の情報は届いていないからだよ。',) if language == 'ja'
                else ("My conversation context doesn't include the server location.",))
    return ()


def grounded_choices(mode, comment, recent, labels, language):
    prior_question = recent[-2][1] if len(recent)>=2 and recent[-2][0]=='user' else ''
    if mode == 'day' and _SIMPLE_DAY.fullmatch(comment.strip().rstrip('?!？。！.')):
        topic = next((name for name, _ in labels if name in _TOPIC_NAMES), None)
        if language == 'ja':
            if topic:
                subject = _TOPIC_NAMES[topic]
                return (f'今日は{subject}の話ができて楽しかったよ。', f'{subject}の話で、ちょっと楽しい一日になったよ。')
            return ('今日はこうして配信でおしゃべりしてるよ。', '今はここで、あなたのコメントに答えてるよ。')
        if topic:
            subject = topic.replace('_', ' ')
            return (f"We've been chatting about {subject} today.", f"I've enjoyed talking about {subject} with you.")
        return ("Right now I'm chatting with you on stream.", "I'm here answering your comments today.")
    if _BARE_PRIVACY.fullmatch(comment) and kind(prior_question)=='other_conversation':
        return (('秘密というより、今の返答では他の人の会話を参照できないんだ。',
                 '隠しているわけじゃないよ。他の人と何を話したかは、ここでは確かめられないよ。')
                if language=='ja' else ("It isn't a secret. I can't look up other viewers' conversations here.",
                                        "I simply can't verify what was discussed with someone else."))
    if mode=='other_conversation':
        return boundary_choices(language)
    if mode=='shift' and _BARE_SHIFT.fullmatch(comment):
        excluded=set(topics_in(comment))
        fresh=[row for row in _STARTERS if row[0] not in excluded and row[0] not in {t for t,n in labels if n>=2}]
        options=fresh or [row for row in _STARTERS if row[0] not in excluded]
        return tuple(row[1 if language=='ja' else 2] for row in options)
    return ()


def boundary_choices(language):
    if language == 'ja':
        return ('他の人との会話の内容は分からないよ。','ここで参照できるのは、あなたとの会話だけだよ。')
    return ("I can't see my conversations with other viewers here.","I can only refer to our conversation here.")


def position_choices(text):
    # A request for alternatives, an explanation or a compound answer needs free speech.
    question=text.strip()
    english = re.match(r'^do you (?:really )?like\s',question,re.I)
    if not english and not (question.endswith(('?', '？')) or question.rstrip('。').endswith('ですか')):
        return ()
    line=question.rstrip('？?。.!')
    if (re.search(r'(?:私|わたし|ぼく|僕|俺|彼|彼女|あの人|その人|母|父|友達|みんな|さん|ちゃん|くん)(?:は|が|の)|'
                  r'覚えて|知って|教えて|以前|前回|だった|と聞|かどうか|以外|好きな',line) or
            kind(line+'？') != 'stance'):
        return ()
    if (len(line)>85 or re.search(r'[、,。?!？！「」]|それとも|どっち|どちら|理由|なぜ|なんで|どうして|どこが|どんな|どの|何が|何を|どれ|'
                                   r"\b(?:or|why|what|which|explain|and|more|less|how|if|prefer|don't|not)\b",line,re.I)):
        return ()
    choices=''
    if re.search(r'(?:は|が|を)?(?:欲しい|ほしい)(?:ですか|の)?$',line):
        choices='want'
    elif re.search(r'(?:して|やって|って|作り|やり|試し|し)たい(?:ですか|の)?$',line):
        choices='try'
    elif re.search(r'好き(?:ですか|なの|か)?$',line) or re.match(r'^do you (?:really )?like\s',line,re.I):
        choices='like'
    if not choices:
        return ()
    lang='ja' if re.search(r'[ぁ-ヿ一-龯]',line) else 'en'
    return _POSITIONS[choices][lang]

_EXTRA = (('winter clothes', re.compile(r'冬服|冬物|コート|マフラー|\b(?:winter coat|winter clothes|winter jacket|scarf)\b', re.I)),)

def kind(text):
    if _OTHER.search(text):
        if re.search(r'(?:人|ひと).{0,8}(?:話|会話)|\b(?:talk|chat|discuss|tell|say)\w*\b',text,re.I):
            return 'other_conversation'
        return ''
    if re.fullmatch(r'\s*(?:なぜ|なんで|どうして|why|why is that)[？?。!！. ]*',text,re.I):
        return 'reason'
    if re.search(r'今日はどんな(?:日|一日)|今日(?:は)?何して|\b(?:what (?:do you do|are you doing|have you been doing) today|how (?:is|was) your day)\b',text,re.I):
        return 'day'
    if re.fullmatch(r'(?:もう|すっかり)?(?:[春夏秋冬]|いい天気|[暑寒涼暖]い|雪|雨)(?:だ|です)?(?:ね|なあ|ですね)[。！!～〜 ]*',text):
        return 'season'
    if _RECAP.search(text):
        return 'recap'
    if _JOKE.search(text):
        return 'joke'
    if _STORY.search(text):
        return 'story'
    if _SHIFT.search(text) and '変えない' not in text and '以外にない' not in text:
        return 'shift'
    if _WISH.search(text):
        return 'wish'
    if _STANCE.search(text) and (text.rstrip().endswith(('?', '？','ですか')) or re.match(r'^(?:do|would) you\b',text,re.I)) and not re.search(r'なぜ|なんで|どうして|どこが|\bwhy\b',text,re.I):
        return 'stance'
    return ''

def session_topics(store, binding, turn):
    """Only labels leave here. Never restore raw dialogue to the four-pair prompt."""
    viewer, stream = binding['key']
    with store.connect() as db:
        rows = db.execute("""SELECT t.comment,t.reply FROM conversation_turns current
            JOIN studio_inputs i ON i.session=current.usage_session
            JOIN conversation_turns t ON t.turn_id=i.turn_id AND t.source_event_id=i.receipt
            JOIN source_events s ON s.id=i.receipt
            WHERE current.turn_id=? AND t.viewer=current.viewer AND t.viewer=? AND s.stream_id=?
              AND s.viewer_key=? AND s.platform='twitch' AND i.source='twitch' AND i.test=0
              AND i.played_at BETWEEN ? AND ? AND i.received < ?
              AND t.status='completed' AND t.played=1 AND s.text=t.comment
              AND s.is_deleted=0 AND s.raw_purged_at IS NULL AND s.expires_at>?
              AND json_extract(t.metrics,'$.session_window.revision')=?
              AND (SELECT version FROM session_evidence_revision WHERE id=1)=?
            ORDER BY i.received DESC LIMIT 20""",
            (turn,viewer,stream,viewer.partition(':')[2],binding['received']-store.window.idle,
             binding['received'],binding['received'],time.time(),binding['revision'],binding['revision'])).fetchall()
    counts = Counter()
    for question, answer in rows:
        if (sensitive(question) or sensitive(answer) or source_block_reason(question)
                or source_block_reason(answer) or name_command(question)[0]):
            continue
        line = question+' '+answer
        labels = tuple(dict.fromkeys((*topics_in(line), *(label for label, rx in _EXTRA if rx.search(line)))))
        counts.update(labels)
    return tuple(counts.most_common(4)), len(rows)

def direction(mode, topics, turn_count, requested=(), language='ja'):
    if mode=='other_conversation':
        return 'State what conversation evidence you can access. You cannot tell whether other conversations took place.'
    if mode=='day':
        labels=', '.join(t for t,_ in topics[:2])
        return (('Mention one subject we actually discussed: '+labels+'.') if labels else
                'Say what interests you in this chat right now, or choose a concrete subject you want to discuss.') + (
                ' Speak only about this exchange. Do not invent work, monitoring, or a day spent watching data.')
    if mode=='season':
        return 'Share one concrete seasonal association as an idea or observation about people. Ray has no skin, breath, nose or live view of the weather.'
    if mode=='choice_reason':
        return ('Give one short reason why YOU like the named subject. Name one feature of it and why that appeals to you. '
                'The viewer sent text. You cannot hear their voice or see their face.')
    if mode=='reason':
        return ('Explain your last answer. If you lacked information, say what information is missing; never infer its cause. '
                'If it was about wearing, owning or trying something, name a concrete feature or design you like or dislike. '
                'Keep the subject of the last exchange; avoid a stock explanation of being an AI.')
    if mode=='stance':
        return 'Give your own yes/no or choice as the first spoken phrase. A reason can follow.'
    if mode=='recap':
        labels=', '.join(t for t,_ in topics[:3])
        return ('Briefly summarize our chat. Include these acknowledged subjects: '+labels+'.'
                if labels else 'Summarize only subjects supported by this conversation.')
    avoid=', '.join(t for t,n in topics if n>=2 and t not in requested)
    novelty=(' For an open invitation, leave out these repeated subjects: '+avoid+'.') if avoid else ''
    if mode in ('story','joke'):
        if requested:
            return 'Use the requested subject for an actual short story or joke with a concrete ending.'
        book=_BITS[mode]
        line=book[(turn_count + (mode=='joke')*2) % len(book)][0 if language=='ja' else 1]
        return ('Perform this short original stage bit, preserving its setup and punchline. '
                'Use the selected language. Here is the entire bit: '+line+novelty)
    if mode=='shift':
        angles=(('stargazing','how two people can draw different constellations from the same stars'),
                ('technology','an undo button on an elevator that lets you cancel a wrong floor'),
                ('gardening','one small pot growing a mint cutting on a windowsill'))
        subject=next((angle for label,angle in angles if label not in requested and
                      label not in {t for t,n in topics if n>=2}), 'the different sounds shoes make on sand and gravel')
        return ('Begin by talking about the new subject itself, for example '+subject+
                '. Name a concrete object and what interests you about it.'+novelty)
    if mode=='wish':
        return ('Name one specific creative thing Ray would like to make or try, in the first short sentence. '
                'A dream may be hypothetical. On a follow-up, develop the wish you just expressed.'+novelty)
    return ''

def apply_plan(plan, mode, instruction):
    if plan is None or not instruction:
        return plan
    if mode in ('shift','story','joke','wish','recap','day','season','reason','choice_reason','self_disclosure'):
        return replace(plan,intent='answer',dialogue_policy='DEEP_DIVE',project='',topic='',
                       preferences=(),hook='',opening_angle='',delivery=instruction)
    return replace(plan,delivery=instruction)
