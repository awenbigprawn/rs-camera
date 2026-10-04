import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import pipeline
import reproduce


class PipelineTests(unittest.TestCase):
    def initialize(self, root, active=None):
        (root/'entrypoints').mkdir()
        (root/'completed').mkdir()
        for stage in reproduce.STAGES:
            (root/'entrypoints'/f'{stage}.sh').write_text('#!/bin/sh\ntrue\n')
        pipeline.save(root/'prepared.json',{})
        pipeline.save(root/'pipeline.json',dict(repo=str(pipeline.REPO),status='queued',active_stage=active,pending_boot=None,pdf=True))

    def mark(self, root, stage):
        pipeline.save(root/'completed'/f'{stage}.json',dict(stage=stage,
                      adapter_sha256=reproduce.digest(root/'entrypoints'/f'{stage}.sh')))

    def test_all_boot_phases_resume_once_and_analyze_only_after_all_captures(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); self.initialize(root)
            machine={'variant':None,'threaded':False,'boot':'initial'}
            captures=[];commands=[];analyses=[];renders=[]
            def boot_matches(variant,threaded):
                return (variant,threaded)==(machine['variant'],machine['threaded'])
            def py(script,*args,**kwargs):
                if script=='reproduce.py' and args[-2]=='run':
                    stage=args[-1]
                    if stage=='e3':
                        self.assertTrue(pipeline.complete(root,'e1'))
                        self.assertTrue(pipeline.complete(root,'e2-calibrate'))
                    if stage.startswith('e4-') and stage!='e4-calibrate':
                        self.assertTrue(pipeline.complete(root,'e4-calibrate'))
                    captures.append(stage); self.mark(root,stage)
                if script=='render.py': renders.append(args[0])
            def analyze(source,out,*args):
                self.assertTrue(all(pipeline.complete(root,s) for s in reproduce.STAGES))
                analyses.append(out); pipeline.save(out/'manifest.json',{})
            with patch.object(pipeline,'require_pi'), patch.object(pipeline,'check_wired'), patch.object(pipeline.os,'geteuid',return_value=0), \
                 patch.object(pipeline,'verify_sources'), patch.object(pipeline,'refresh_boot_config'), \
                 patch.object(pipeline,'restore_system') as restore, patch.object(pipeline,'boot_matches',side_effect=boot_matches), \
                 patch.object(pipeline,'current_boot_id',side_effect=lambda:machine['boot']), \
                 patch.object(pipeline,'py',side_effect=py), patch.object(pipeline,'call',side_effect=lambda cmd,**kw:commands.append(cmd)), \
                 patch.object(pipeline,'analyze',side_effect=analyze):
                for n in range(6):
                    pipeline.worker(root)
                    state=pipeline.read(root/'pipeline.json')
                    if state['status']=='complete': break
                    self.assertEqual(state['status'],'rebooting')
                    machine.update(variant=state['pending_boot']['variant'],threaded=state['pending_boot']['threaded'],boot=f'boot-{n}')
                self.assertEqual(state['status'],'complete')
                self.assertEqual(len(captures),len(reproduce.STAGES))
                self.assertEqual(set(captures),set(reproduce.STAGES))
                self.assertEqual(len(analyses),1); self.assertEqual(len(renders),1)
                self.assertEqual(commands.count(['systemctl','reboot']),5)
                restore.assert_called_once_with(root)

    def test_failed_boot_does_not_create_a_reboot_loop(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.initialize(root)
            state=pipeline.read(root/'pipeline.json')
            state['pending_boot']=dict(variant='rt-urb16',threaded=False,boot_id='before')
            pipeline.save(root/'pipeline.json',state)
            with patch.object(pipeline,'require_pi'), patch.object(pipeline,'check_wired'),patch.object(pipeline.os,'geteuid',return_value=0), \
                 patch.object(pipeline,'verify_sources'),patch.object(pipeline,'current_boot_id',return_value='after'), \
                 patch.object(pipeline,'boot_matches',return_value=False),patch.object(pipeline,'restore_system'), \
                 patch.object(pipeline,'call') as call:
                with self.assertRaisesRegex(ValueError,'automatic reboot retry disabled'):pipeline.worker(root)
                self.assertNotIn(unittest.mock.call(['systemctl','reboot']),call.call_args_list)
                self.assertEqual(pipeline.read(root/'pipeline.json')['status'],'failed')

    def test_interrupted_acquisition_is_not_silently_repeated(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.initialize(root,active='e1')
            with patch.object(pipeline,'require_pi'), patch.object(pipeline,'check_wired'),patch.object(pipeline.os,'geteuid',return_value=0), \
                 patch.object(pipeline,'verify_sources'),patch.object(pipeline,'restore_system'), \
                 patch.object(pipeline,'call'),patch.object(pipeline,'py') as py:
                with self.assertRaisesRegex(ValueError,'interrupted'):pipeline.worker(root)
                py.assert_not_called()

    def test_changed_completion_marker_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);self.initialize(root);self.mark(root,'e1')
            (root/'entrypoints/e1.sh').write_text('changed')
            with self.assertRaisesRegex(ValueError,'completion marker'):pipeline.complete(root,'e1')

    def test_boot_modes_distinguish_hardirq_and_threadirqs(self):
        release=pipeline.VARIANTS['standard-urb16'][0]
        self.assertTrue(pipeline.boot_matches('standard-urb16',False,release,'root=x'))
        self.assertFalse(pipeline.boot_matches('standard-urb16',False,release,'root=x threadirqs'))
        self.assertTrue(pipeline.boot_matches('standard-urb16',True,release,'root=x threadirqs'))
        self.assertTrue(pipeline.boot_matches('rt-urb16',False,pipeline.VARIANTS['rt-urb16'][0],'root=x'))

    def test_prepare_irq_refresh_preserves_physical_selection(self):
        cfg=reproduce.load_config(ROOT/'setup/config.pi5.json')
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); reproduce.prepare(ROOT.parent,root,cfg)
            updated=json.loads(json.dumps(cfg))
            updated['cameras']['d435_b']['xhci_irq']=88
            updated['e3_irqs']['d435']['number']=88
            with self.assertRaises(ValueError):reproduce.prepare(ROOT.parent,root,updated)
            reproduce.prepare(ROOT.parent,root,updated,allow_irq_refresh=True)
            self.assertIn('xhci_irq=88',(root/'entrypoints/e3-one.sh').read_text())
            updated['cameras']['d435_b']['serial']='999999'
            with self.assertRaises(ValueError):reproduce.prepare(ROOT.parent,root,updated,allow_irq_refresh=True)

    def test_unsafe_systemd_paths_rejected(self):
        for path in ('/tmp/has space','/tmp/%n','/tmp/$USER'):
            with self.subTest(path=path),self.assertRaises(ValueError):pipeline.systemd_path(Path(path))

if __name__=='__main__': unittest.main()
