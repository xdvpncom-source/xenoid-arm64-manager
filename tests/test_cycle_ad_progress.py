"""Timer-aware ad choices: real Close first; bounded, explicit Back last."""
import copy
import unittest
from unittest.mock import patch
from test_cycle_missing_close import AD, Clock, Device, MAIN, m


def node(text='', *, clickable=False, enabled=True, cls='android.widget.TextView', rid='qa:id/timer', box=None):
    return {'text':text,'content_desc':'','resource_id':rid,'class_name':cls,'bounds':box or [2,2,90,12],
            'clickable':clickable,'enabled':enabled,'package':'v2.vpn.fast'}


class LoggedDevice(Device):
    def __init__(self):super().__init__();self.logs=[]
    def log(self,text):self.logs.append(text)


class Tests(unittest.TestCase):
    def setUp(self):
        self.clock=Clock();self.device=LoggedDevice();self.cycle=m.Cycle(self.device,'v2.vpn.fast')
        self.time=patch.object(m,'time',self.clock);self.time.start();self.addCleanup(self.time.stop)

    def ad(self,*nodes):
        value=copy.deepcopy(AD);value['controls']=list(nodes);return value

    def service(self,window):
        self.device.window=window;return self.cycle.service_missing_ad_close(self.cycle.observe())

    def test_enabled_looking_skip_in5_is_not_close_until_zero(self):
        for label in ('Skip in 5','Close in 3','Skip after 8 seconds'):
            with self.subTest(label=label):
                window=self.ad(node(label,clickable=True,cls='android.widget.Button'))
                self.assertIsNone(self.cycle.ad_close_control(window))
                self.assertFalse(self.service(window));self.clock.now+=16
                self.assertFalse(self.service(window))
        zero=self.ad(node('Skip in 0',clickable=True,cls='android.widget.Button'))
        self.assertIsNotNone(self.cycle.ad_close_control(zero));self.assertFalse(self.device.taps)

    def test_disabled_skip_countdown_waits_then_enabled_close_is_chosen(self):
        value=self.ad(node('5'),node('Skip',clickable=True,enabled=False,cls='android.widget.Button'))
        self.assertIsNone(self.cycle.ad_close_control(value));self.assertFalse(self.service(value))
        self.clock.now=16;self.assertFalse(self.service(value));self.assertFalse(self.device.taps)
        done=self.ad(node('0'),node('Close',clickable=True,cls='android.widget.Button',rid='qa:id/close'))
        self.assertEqual(self.cycle.ad_close_control(done)['text'],'Close')

    def test_zero_timer_label_cannot_override_conflicting_install_action(self):
        value=self.ad({**node('Install',clickable=True,cls='android.widget.Button'), 'content_desc':'Skip in 0'})
        self.assertIsNone(self.cycle.ad_close_control(value))

    def test_changing_numeric_countdown_blocks_back_beyond_unknown_budget(self):
        for now,seconds in ((0,20),(20,19),(40,18),(60,17),(80,16)):
            self.clock.now=now;self.assertFalse(self.service(self.ad(node(str(seconds)))))
        self.assertFalse(self.device.taps)

    def test_changing_known_progress_blocks_then_finished_progress_starts_grace(self):
        for now,value in ((0,10),(20,40),(40,80)):
            self.clock.now=now;self.assertFalse(self.service(self.ad(node(str(value)+'%',cls='android.widget.ProgressBar'))))
        done=self.ad(node('100%',cls='android.widget.ProgressBar'))
        self.clock.now=50;self.assertFalse(self.service(done))
        self.clock.now=66;self.assertTrue(self.service(done));self.assertEqual(self.device.taps,[(42,345)])

    def test_real_enabled_close_wins_over_decorative_progress(self):
        value=self.ad(node('',cls='android.widget.ProgressBar'),node('Close',clickable=True,cls='android.widget.Button'))
        self.assertEqual(self.cycle.ad_close_control(value)['text'],'Close')
        self.assertFalse(self.cycle.wait_for_ad_progress(value))

    def test_stalled_progress_logs_unresolved_then_allows_only_observed_back(self):
        value=self.ad(node('',cls='android.widget.ProgressBar'))
        with patch.object(m,'AD_PROGRESS_STALL_SECONDS',10):
            self.assertFalse(self.service(value));self.clock.now=11;self.assertFalse(self.service(value))
            self.clock.now=27;self.assertTrue(self.service(value))
        self.assertEqual(self.device.taps,[(42,345)])
        self.assertTrue(any('progress unresolved' in x for x in self.device.logs))
        self.assertFalse(any('timer finished' in x for x in self.device.logs))

    def test_unknown_budget_is_script_controlled_and_no_nav_has_no_fallback(self):
        value=self.ad(node('',cls='android.widget.ProgressBar'));value['navigation_controls']=[]
        with patch.object(m,'AD_PROGRESS_STALL_SECONDS',0),patch.object(m,'AD_PROGRESS_WAIT_SECONDS',5):
            self.assertFalse(self.service(value));self.clock.now=6;self.assertFalse(self.service(value))
            self.clock.now=22;self.assertFalse(self.service(value))
        self.assertFalse(self.device.taps)

    def test_confirmed_close_does_not_lend_exhausted_progress_budget_to_next_ad(self):
        progress=self.ad(node('',cls='android.widget.ProgressBar'))
        self.assertTrue(self.cycle.wait_for_ad_progress(progress))
        self.clock.now=31;self.assertFalse(self.cycle.wait_for_ad_progress(progress))
        closing=self.ad(node('Close',clickable=True,cls='android.widget.Button'))
        self.cycle.close_ad_control(closing,self.cycle.ad_close_control(closing))
        self.clock.now=32;self.assertTrue(self.cycle.wait_for_ad_progress(progress))
        self.assertEqual(self.cycle.ad_progress_wait['started'],32)

    def test_nonclickable_close_label_is_explicitly_listed_and_preserved(self):
        value=self.ad(node('Close',clickable=False))
        selected=self.cycle.ad_close_control(value)
        self.assertEqual(selected,value['controls'][0])
        self.assertFalse(selected['clickable'])
        self.assertTrue(any('semantic_label' in x for x in self.device.logs))

    def test_inventory_has_no_quota_and_ephemeral_ids_do_not_repeat_logging(self):
        value=self.ad(*(node(str(i),clickable=True,rid='qa:id/c'+str(i))for i in range(300)))
        self.cycle.ad_close_control(value)
        count=sum(x.startswith('Ad control ')for x in self.device.logs)
        self.assertEqual(count,300)
        for control in value['controls']:control['window_id']=999
        self.clock.now=20;self.cycle.ad_close_control(value)
        self.assertEqual(sum(x.startswith('Ad control ')for x in self.device.logs),count)


if __name__=='__main__':unittest.main()
