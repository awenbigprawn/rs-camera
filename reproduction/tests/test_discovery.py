import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('camera_discovery', ROOT/'setup/discover.py')
discovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(discovery)


def device(serial, model, irq, speed='5000'):
    return dict(serial=serial, model=model, xhci_irq=irq, xhci_label=f'xhci-hcd:usb{irq}',
                usb=dict(speed_mbps=speed))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.devices = [device('300','D455F',132), device('200','D435',137), device('100','D435',132)]

    def test_selects_different_controllers_independent_of_enumeration_order(self):
        config = discovery.make_config(self.devices)
        self.assertEqual(config,discovery.make_config(list(reversed(self.devices))))
        self.assertEqual(config['selection']['d435_pair'],['d435_100','d435_200'])
        self.assertEqual(config['selection']['single_d455'],'d455f_300')

    def test_retain_custom_roles_and_aliases(self):
        old = {'cameras':{'left':dict(serial='200'),'right':dict(serial='100'),'thermal':dict(serial='300')},
               'selection':dict(d435_pair=['left','right'],single_d435='left',single_d455='thermal',startup_d435='right')}
        config=discovery.make_config(self.devices,old)
        self.assertEqual(config['selection'],old['selection'])
        self.assertEqual(config['cameras']['left']['serial'],'200')

    def test_missing_selected_camera_requires_explicit_reselection(self):
        old=discovery.make_config(self.devices)
        with self.assertRaisesRegex(ValueError,'previously selected camera'):
            discovery.make_config([*self.devices[:2],device('101','D435',132)],old)

    def test_usb2_or_shared_controller_or_missing_model_rejected(self):
        for devices in ([device('1','D435',1),device('2','D435',1),device('3','D455',2)],
                        [device('1','D435',1,'480'),device('2','D435',2),device('3','D455',2)],
                        self.devices[1:]):
            with self.subTest(devices=devices),self.assertRaises(ValueError):
                discovery.make_config(devices)

    def test_sdk_serial_is_not_usb_descriptor_serial(self):
        with tempfile.TemporaryDirectory() as temp:
            usbpath=Path(temp)/'xhci-hcd.0/usb2/2-1'
            usbpath.mkdir(parents=True)
            sdk=[dict(serial='123',name='Intel RealSense D435',physical_port=str(usbpath/'2-1:1.0/video4linux/video0'))]
            usb=[dict(serial='999',usb_device='2-1',resolved_sysfs_path=str(usbpath))]
            irqs=[dict(irq=132,action='xhci-hcd:usb1',controller=str(usbpath.parent.parent),usb_devices=['2-1'])]
            cameras,issues=discovery.inventory(sdk,usb,irqs)
            self.assertFalse(issues)
            self.assertEqual(cameras[0]['serial'],'123')
            self.assertEqual(cameras[0]['xhci_label'],'xhci-hcd:usb1')
            self.assertEqual(cameras[0]['usb']['serial'],'999')
            # A same-serial device on a different physical path must not match.
            sdk[0]['physical_port']=temp+'/other/video0'
            cameras,issues=discovery.inventory(sdk,usb,irqs)
            self.assertTrue(issues)
            self.assertNotIn('xhci_irq',cameras[0])

    def test_generated_config_is_accepted_by_existing_runner(self):
        sys.path.insert(0,str(ROOT))
        import reproduce
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'config.json'
            discovery.save_json(path,discovery.make_config(self.devices))
            self.assertEqual(reproduce.load_config(path)['d435_serials'],['100','200'])


class DiscoveryCliTests(unittest.TestCase):
    def test_no_devices_preserves_config_and_records_inventory(self):
        import json
        import io
        from unittest.mock import patch
        from subprocess import CompletedProcess
        with tempfile.TemporaryDirectory() as temp:
            config=Path(temp)/'config.json'; inventory=Path(temp)/'inventory.json'
            before=json.dumps(discovery.make_config([device('1','D435',1),device('2','D435',2),device('3','D455',2)]))
            config.write_text(before)
            argv=['discover','--output',str(config),'--inventory',str(inventory)]
            with patch.object(sys,'stderr',io.StringIO()), patch.object(sys,'argv',argv), patch.object(discovery.subprocess,'run',return_value=CompletedProcess([],0,'{"devices":[]}','')), patch.object(discovery,'discover_realsense_devices',return_value=[]), patch.object(discovery.CpuIsolation,'discover_camera_xhci_irqs',return_value=[]):
                self.assertEqual(discovery.main(),1)
            self.assertEqual(config.read_text(),before)
            self.assertFalse(json.loads(inventory.read_text())['ready'])
            self.assertEqual(list(Path(temp).glob('config.previous-*')),[])

if __name__=='__main__': unittest.main()
