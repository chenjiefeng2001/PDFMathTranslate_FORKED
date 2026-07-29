import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from pdf2zh.v3.service import ServiceRegistry, ServiceInterface, ParserService, TranslatorService, LayoutService
    _HAS = True
except ImportError as e:
    _HAS = False
    print(f"Service import error: {e}")

@unittest.skipIf(not _HAS, "not importable")
class TestServiceInterface(unittest.TestCase):
    def test_base(self):
        si = ServiceInterface()
        self.assertIsNotNone(si)

@unittest.skipIf(not _HAS, "not importable")
class TestServiceRegistry(unittest.TestCase):
    def setUp(self):
        ServiceRegistry.reset_instance()
        self.r = ServiceRegistry.get_instance()
    def test_singleton(self):
        r2 = ServiceRegistry.get_instance()
        self.assertIs(self.r, r2)
    def test_reset(self):
        ServiceRegistry.reset_instance()
        self.assertIsNone(ServiceRegistry._instance)
    def test_register_and_get(self):
        obj = object()
        self.r.register(ParserService, obj)
        self.assertIs(self.r.get(ParserService), obj)
    def test_register_replace(self):
        self.r.register(ParserService, "v1")
        self.r.register(ParserService, "v2", replace=True)
        self.assertEqual(self.r.get(ParserService), "v2")
    def test_get_missing_raises(self):
        with self.assertRaises(KeyError):
            self.r.get(TranslatorService)
    def test_get_or_default(self):
        val = self.r.get_or_default(TranslatorService, "fallback")
        self.assertEqual(val, "fallback")
    def test_has(self):
        self.r.register(ParserService, object())
        self.assertTrue(self.r.has(ParserService))
    def test_has_not(self):
        self.assertFalse(self.r.has(LayoutService))
    def test_list_all(self):
        self.r.register(ParserService, object())
        self.r.register(TranslatorService, object())
        self.assertEqual(len(self.r.list_services()), 2)
    def test_clear(self):
        self.r.register(ParserService, object())
        self.r.clear()
        self.assertEqual(len(self.r.list_services()), 0)
    def test_register_factory(self):
        self.r.register_factory(ParserService, lambda: "from_factory")
        val = self.r.get(ParserService)
        self.assertEqual(val, "from_factory")
    def test_replace_method(self):
        self.r.register(ParserService, "old")
        self.r.replace(ParserService, "new")
        self.assertEqual(self.r.get(ParserService), "new")
    def test_unregister(self):
        self.r.register(ParserService, object())
        self.r.unregister(ParserService)
        self.assertFalse(self.r.has(ParserService))
