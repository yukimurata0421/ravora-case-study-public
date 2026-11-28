import unittest

from ravora.persona import NAME_ORIGIN
from ravora.ray_self_disclosure import disclosure


def guidance(q, history):
    return disclosure(q, history, 'ja')[0]


class RaySelfDisclosureTests(unittest.TestCase):
    def test_bilingual_public_questions(self):
        for text, fact in (
            ('レイの意外な一面を教えて', 'constellations'),
            ('秘密の話を教えて', 'constellations'), ('あなたの秘密は？', 'constellations'),
            ("What's your secret?", 'constellations'),
            ('自分のことをどう思っている？', 'competitive'),
            ('What do you think of yourself?', 'competitive'),
            ('あなたは機能のひとつなの？', 'AI host'),
            ('名前ってどうやって決まったの？', NAME_ORIGIN), ('なんでレイって名前にしたの？', NAME_ORIGIN),
            ('レイの名前の由来は？', NAME_ORIGIN), ('Who chose your name?', NAME_ORIGIN),
        ):
            with self.subTest(text=text):
                self.assertIn(fact, guidance(text, ()))

    def test_short_openings_and_unscripted_followups(self):
        q = 'あなたの秘密は？'
        guidance, options = disclosure(q, (), 'ja')
        self.assertTrue(options and all('星座' in s for s in options))
        history = [('user', q), ('model', options[0])]
        self.assertEqual(disclosure(q, history, 'ja')[1], ())
        self.assertEqual(disclosure('たとえば？', history, 'ja')[1], ())
        self.assertTrue(all(s.isascii() for s in disclosure("What's your secret?", (), 'en')[1]))

    def test_followups_keep_the_viewpoint_of_the_last_subject(self):
        history=[('user', 'あなたの秘密は？'), ('model', '星座に別の名前をつけるのが好き。')]
        self.assertIn('constellations', guidance('なんで？', history))
        self.assertIn("hasn't heard", guidance('秘密の話を教えて', history))
        history.extend([('user', 'なんで？'), ('model','同じ星から別の絵を想像できるから。')])
        example=guidance('例えば？', history)
        self.assertIn('constellations', example)
        self.assertIn('concrete example', example)
        history.extend([('user','How would you describe yourself?'), ('model','I get competitive.')])
        self.assertIn('competitive', guidance('Why?', history))
        self.assertNotIn('constellations', guidance('Like what?', history))

    def test_question_must_refer_to_ray_and_a_followup_needs_recent_evidence(self):
        for line in ('他の人の秘密を教えて', '私の名前の由来は？','秘密鍵は？','サーバーの秘密は？',
                     '昨日星を見たの？','What is her secret?', '私のことをどう思っている？'):
            with self.subTest(line=line):
                self.assertFalse(guidance(line, ()))
        self.assertFalse(guidance('なんで？', ()))
        prior=[('user','レイの名前の由来は？'),('model','短い名前がよくて、自分で提案したよ。')]
        self.assertIn(NAME_ORIGIN, guidance('例を教えて', prior))
        prior.extend([('user','サーバーってどこ？'),('model','所在地の情報は受け取っていないよ。')])
        self.assertFalse(guidance('なんで？',prior))


class RayMotivationTests(unittest.TestCase):
    def test_distinct_reasons_and_examples_in_either_order_across_rolling_window(self):
        for start, asks, expected in (
            ('レイって負けず嫌い？', ('なんで？', 'たとえば？', 'なんで？', 'たとえば？', 'なんで？', 'たとえば？'),
             ('負けず嫌い', 'まぐれ', '鍵の音', '手がかり', '一本', '説明', '消え方')),
            ('なんのために配信しているの？', ('たとえば？', 'たとえば？', 'なんで？', 'なんで？', 'もう一つ例は？', 'どうして？'),
             ('コメント', '雨よけ', '持ち手', 'ひとり', '足し', '最後', '瞬間')),
        ):
            recent = []
            for q, idea in zip((start, *asks), expected, strict=True):
                _, choices = disclosure(q, recent[-8:], 'ja')
                self.assertTrue(choices, q)
                self.assertIn(idea, choices[0])
                recent.extend((('user', q), ('model', choices[0])))
            self.assertEqual(disclosure(asks[-1], recent[-8:], 'ja')[1], ())

    def test_scope_changes_and_acknowledged_evidence(self):
        options = disclosure('Why do you stream?', (), 'en')[1]
        self.assertTrue(options[0].isascii())
        prior = [('user', 'Why do you stream?'), ('model', options[0])]
        self.assertIn('On my own', disclosure('Why?', prior, 'en')[1][0])
        q = 'レイの負けず嫌いの中身を教えて'
        _, choices = disclosure(q, prior, 'ja')
        self.assertIn('悔し', choices[0])
        prior.extend((('user', q), ('model', choices[0])))
        self.assertIn('鍵', disclosure('例えば？', prior, 'ja')[1][0])
        for other in ('私が負けず嫌いなのはなぜ？', '友達が配信をする理由は？',
                      'レイは負けず嫌い？それと明日の予定は？', 'Why does she stream?'):
            self.assertEqual(disclosure(other, prior, 'ja'), ('', ()))
            self.assertEqual(disclosure('なんで？', [*prior, ('user', other), ('model', '別の話。')], 'ja'), ('', ()))
        self.assertEqual(disclosure('なんで？', [('user', q), ('model', '')], 'ja'), ('', ()))

    def test_general_self_evaluation_can_lead_into_competition(self):
        q = '自分のことをどう思っている？'
        for answer in disclosure(q, (), 'ja')[1]:
            _, choices = disclosure('たとえば？', [('user', q), ('model', answer)], 'ja')
            self.assertIn('鍵', choices[0])
