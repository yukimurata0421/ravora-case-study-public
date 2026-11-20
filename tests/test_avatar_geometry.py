"""Portable geometry checks with synthetic mouth grids."""
import unittest

from ravora.pngtuber import _face_midline, _lip_thickness, lip_thickness_ratio


class AvatarGeometryTests(unittest.TestCase):
    def test_four_landmarks_must_agree(self):
        marks={'nose':[50,20],'upper_lip':[52,40],'lower_lip':[53,50],'chin':[56,80]}
        intercept,slope=_face_midline(marks,100,100)
        self.assertAlmostEqual(intercept,48)
        self.assertAlmostEqual(slope,.1)
        with self.assertRaisesRegex(ValueError,'DISAGREES'):
            _face_midline({**marks,'lower_lip':[66,50]},100,100)

    def test_lip_thickness_changes_outer_edges_only(self):
        cols,rows=9,7
        rest=[v for row in range(rows) for col in range(cols) for v in (10+10*col,10+10*row)]
        target=rest.copy()
        for col in range(1,cols-1):
            target[2*(cols+col)+1]-=4
            target[2*(4*cols+col)+1]+=4
        feature=dict(kind='mouth',image='vowel_A',columns=cols,rows=rows,source=rest,target=target,
                     interior_rows=[2,3],region=[50,40,40,30])
        for vowel,upper_bound in [('A',1.1),('O',1.3)]:
            corrected=_lip_thickness(feature,target,vowel,100,100)
            for ratio in lip_thickness_ratio(rest,corrected,cols):
                self.assertAlmostEqual(ratio,upper_bound)
            for row in (0,2,3,5,6):
                self.assertEqual(corrected[row*2*cols:(row+1)*2*cols],rest[row*2*cols:(row+1)*2*cols])
