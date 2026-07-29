import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
_HAS_V3 = False
try:
    from pdf2zh.v3.parser import RawBlock, RawBlockType, RawSpan, PDFParser
    from pdf2zh.v3.normalizer import NormalizedBlock, Normalizer, NormalizerConfig
    from pdf2zh.v3.graph import DocumentNode, DocumentGraph, DocumentGraphBuilder, Edge, EdgeType, NodeType, ConstraintPriority, GraphBuildConfig
    from pdf2zh.v3.analyzer import SemanticAnalyzer, AnalyzerConfig
    from pdf2zh.v3.planner import TranslationPlanner, TranslationPlan, TranslationChunk, ContextWindow, PromptManager, ContextBuilder, GlossaryEntry as PlannerGlossaryEntry, GlossaryManager, ChunkStrategy, ChunkSplitter, PlannerConfig
    from pdf2zh.v3.runtime import GraphRuntime, GraphTransaction, GraphVersion, GraphSnapshot, GraphObserver, ChangeRecord, TransactionStatus
    from pdf2zh.v3.memory import DocumentMemory, DocumentMemorySnapshot, EntityEntry, AbbreviationEntry
    from pdf2zh.v3.visual_tree import VisualTree, VisualNode, VisualNodeType, BoundingBox, Page, Paragraph, Line, TextRun, GlyphRun, Image, Formula
    from pdf2zh.v3.evaluator import EvaluationResult, clamp, TranslationEvaluator, SemanticEvaluator, TypographyEvaluator, LayoutEvaluator, ConsistencyEvaluator
    from pdf2zh.v3.scheduler import Task, TaskStatus, TaskGraph, Executor, Scheduler
    from pdf2zh.v3.service import ServiceRegistry, ServiceInterface, ParserService, AnalyzerService, PlannerService, TranslatorService, LayoutService, RendererService, QAService, MemoryService
    _HAS_V3 = True
except ImportError as e:
    print(f'V3 import error: {e}')


def _make_graph():
    g = DocumentGraph()
    g.add_node(DocumentNode('n1', NodeType.PARAGRAPH, (0,0,100,20), text='Hello', page_num=0, font_size=12.0))
    g.add_node(DocumentNode('n2', NodeType.PARAGRAPH, (0,25,100,45), text='World', page_num=0, font_size=14.0))
    g.add_edge(Edge('n1','n2', EdgeType.FOLLOWS))
    return g

@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestParser(unittest.TestCase):
    def test_rawblock_type_enum(self):
        self.assertIn('TEXT', {m.name for m in RawBlockType})
    def test_rawspan_defaults(self):
        s = RawSpan(text='hello')
        self.assertEqual(s.text, 'hello')
    def test_rawblock_text(self):
        b = RawBlock(block_type=RawBlockType.TEXT, spans=[RawSpan(text='Hello '), RawSpan(text='World')])
        self.assertEqual(b.text, 'Hello World')
    def test_font_size_avg(self):
        b = RawBlock(block_type=RawBlockType.TEXT, spans=[RawSpan(text='A', font_size=12), RawSpan(text='B', font_size=14)])
        self.assertAlmostEqual(b.font_size_avg, 13.0)
    def test_font_size_avg_empty(self):
        b = RawBlock(block_type=RawBlockType.TEXT, spans=[])
        self.assertEqual(b.font_size_avg, 0.0)
    def test_safe_fontname(self):
        self.assertEqual(PDFParser._safe_fontname('C2_0+Times'), 'Times')
    def test_safe_fontname_none(self):
        self.assertEqual(PDFParser._safe_fontname(None), '')



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestNormalizer(unittest.TestCase):
    def test_normalizedblock(self):
        b = NormalizedBlock(text='Hello', bbox=(0,0,100,20), page_num=0, font_size_avg=12.0, font_style=None, font_name_original='')
        self.assertEqual(b.text, 'Hello')
    def test_normalizer_config(self):
        c = NormalizerConfig(lang_in='auto')
        self.assertEqual(c.lang_in, 'auto')
    def test_normalize(self):
        n = Normalizer(NormalizerConfig(lang_in='en'))
        result = n.normalize([RawBlock(RawBlockType.TEXT, spans=[RawSpan(text='Hello World')])])
        self.assertGreater(len(result), 0)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestGraph(unittest.TestCase):
    def test_document_node(self):
        n = DocumentNode('n1', NodeType.PARAGRAPH, (0,0,100,20), text='Hello', page_num=0)
        self.assertEqual(n.text, 'Hello')
    def test_add_get_node(self):
        g = DocumentGraph()
        n = DocumentNode('n1', NodeType.PARAGRAPH, (0,0,10,10), page_num=0)
        g.add_node(n)
        self.assertIsNotNone(g.get_node('n1'))
    def test_edge(self):
        g = DocumentGraph()
        g.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,10,10), page_num=0))
        g.add_node(DocumentNode('b', NodeType.PARAGRAPH, (0,15,10,25), page_num=0))
        e = Edge('a','b',EdgeType.FOLLOWS)
        g.add_edge(e)
        self.assertIn(e, g.edges)
    def test_len(self):
        g = DocumentGraph()
        g.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,10,10), page_num=0))
        g.add_node(DocumentNode('b', NodeType.PARAGRAPH, (0,15,10,25), page_num=0))
        self.assertEqual(len(g), 2)
    def test_getitem(self):
        g = DocumentGraph()
        g.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,10,10), page_num=0))
        self.assertIsNotNone(g['a'])
    def test_builder(self):
        b = DocumentGraphBuilder()
        result = b.build([])
        self.assertIsInstance(result, DocumentGraph)
        self.assertEqual(len(result), 0)
    def test_constraint_priority(self):
        self.assertIn(ConstraintPriority.HARD, ConstraintPriority)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestAnalyzer(unittest.TestCase):
    def test_config(self):
        self.assertEqual(AnalyzerConfig(lang_in='en').lang_in, 'en')
    def test_analyze_empty(self):
        g = SemanticAnalyzer(AnalyzerConfig(lang_in='en')).analyze(DocumentGraph())
        self.assertIsInstance(g, DocumentGraph)
    def test_analyze(self):
        r = SemanticAnalyzer(AnalyzerConfig(lang_in='en')).analyze(_make_graph())
        self.assertIsInstance(r, DocumentGraph)


@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestPlanner(unittest.TestCase):
    def test_config_defaults(self):
        c = PlannerConfig()
        self.assertEqual(c.chunk_strategy, ChunkStrategy.SINGLE)
    def test_plan(self):
        graph = _make_graph()
        first_id = graph.nodes[0].id
        plan = TranslationPlanner(PlannerConfig()).plan(graph, first_id)
        self.assertIsInstance(plan, TranslationPlan)
    def test_context_window(self):
        cw = ContextWindow(preceding_texts=['A'], following_texts=['C'], doc_title='Doc')
        self.assertEqual(cw.doc_title, 'Doc')
    def test_glossary_manager(self):
        gm = GlossaryManager()
        gm.add_term('LLM', '大语言模型')
        entries = gm.get_all_entries()
        self.assertEqual(len(entries), 1)
    def test_chunk_splitter(self):
        cs = ChunkSplitter(max_chars=100)
        chunks = cs.split('Hello world', strategy=ChunkStrategy.SINGLE)
        self.assertEqual(len(chunks), 1)
    def test_prompt_manager(self):
        pm = PromptManager()
        from pdf2zh.v3.planner import NodeType as PlannerNodeType
        self.assertIsNotNone(pm.get_template(PlannerNodeType.PARAGRAPH))
    def test_context_builder(self):
        cb = ContextBuilder(max_preceding=3)
        graph = _make_graph()
        self.assertIsNotNone(cb)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestGraphRuntime(unittest.TestCase):
    def setUp(self):
        self.g = _make_graph()
        self.rt = GraphRuntime(self.g)
    def test_revision_count(self):
        self.assertEqual(self.rt.revision_count, 1)
    def test_transaction_commit(self):
        with self.rt.transaction('tx'):
            self.g.get_node('n1').text = 'translated'
        self.assertEqual(self.rt.revision_count, 2)
    def test_dirty(self):
        self.rt.mark_dirty('n1')
        self.assertTrue(self.rt.is_dirty('n1'))
        self.rt.mark_clean('n1')
        self.assertFalse(self.rt.is_dirty('n1'))
    def test_snapshot(self):
        snap = self.rt.take_snapshot('test')
        self.assertEqual(len(snap.nodes), 2)
    def test_snapshot_restore(self):
        snap = self.rt.take_snapshot('before')
        self.g.get_node('n1').text = 'changed'
        self.rt.restore_snapshot(snap)
        self.assertEqual(self.rt.graph.get_node('n1').text, 'Hello')
    def test_observer(self):
        events = []
        self.rt.observer.on('node_changed', lambda **kw: events.append(kw))
        self.rt.mark_dirty('n1')
        self.assertEqual(len(events), 1)
    def test_version(self):
        v = self.rt.current_revision
        self.assertTrue(self.rt.has_revision(v))
        self.assertFalse(self.rt.has_revision('x'))
    def test_clear_dirty(self):
        self.rt.mark_dirty('n1')
        self.rt.clear_dirty()
        self.assertEqual(len(self.rt.dirty_nodes), 0)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestDocumentMemory(unittest.TestCase):
    def setUp(self):
        self.m = DocumentMemory()
    def test_entity(self):
        self.m.remember_entity('LLM', 'Large Language Model', definition='Model', translation='大语言模型')
        self.assertIsNotNone(self.m.get_entity('llm'))
    def test_entity_via_alias(self):
        self.m.remember_entity('CNN', 'Convolutional Neural Network')
        self.assertIsNotNone(self.m.get_entity('Convolutional Neural Network'))
    def test_has_entity(self):
        self.m.remember_entity('GPU')
        self.assertTrue(self.m.has_entity('gpu'))
        self.assertFalse(self.m.has_entity('TPU'))
    def test_entity_count(self):
        self.m.remember_entity('A'); self.m.remember_entity('B')
        self.assertEqual(self.m.entity_count(), 2)
    def test_glossary(self):
        self.m.remember_glossary('neural machine translation', '神经机器翻译')
        e = self.m.lookup_glossary('Neural Machine Translation')
        self.assertEqual(e.target, '神经机器翻译')
    def test_abbreviation(self):
        self.m.remember_abbreviation('NMT', 'Neural Machine Translation')
        self.assertEqual(self.m.expand_abbreviation('nmt'), 'Neural Machine Translation')
    def test_topics(self):
        self.m.set_topics(['deep learning', 'NLP'])
        self.m.add_topic('translation')
        self.assertIn('NLP', self.m.topics)
    def test_language_style(self):
        self.m.set_language_style('academic')
        self.assertEqual(self.m.language_style, 'academic')
    def test_snapshot(self):
        self.m.remember_entity('LLM', translation='大语言模型')
        snap = self.m.take_snapshot()
        self.m.clear()
        self.m.restore_snapshot(snap)
        self.assertEqual(self.m.entity_count(), 1)
    def test_glossary_pairs(self):
        self.m.remember_glossary('hello', '你好')
        self.assertEqual(self.m.get_glossary_pairs().get('hello'), '你好')
    def test_clear(self):
        self.m.remember_entity('A')
        self.m.clear()
        self.assertEqual(self.m.entity_count(), 0)
    def test_glossary_count(self):
        self.m.remember_glossary('a', 'b')
        self.assertEqual(self.m.glossary_count(), 1)
    def test_abbreviation_count(self):
        self.m.remember_abbreviation('NMT', 'Neural Machine Translation')
        self.assertEqual(self.m.abbreviation_count(), 1)


@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestVisualTree(unittest.TestCase):
    def test_vtype(self):
        self.assertEqual(VisualNodeType.PAGE.value, 'page')
    def test_bbox(self):
        bb = BoundingBox(10,20,100,50)
        self.assertAlmostEqual(bb.x1, 110)
        self.assertAlmostEqual(bb.y1, 70)
    def test_bbox_contains(self):
        self.assertTrue(BoundingBox(10,20,100,100).contains(BoundingBox(20,30,30,20)))
        self.assertFalse(BoundingBox(20,30,30,20).contains(BoundingBox(10,20,100,100)))
    def test_bbox_overlaps(self):
        self.assertTrue(BoundingBox(0,0,50,50).overlaps(BoundingBox(25,25,50,50)))
        self.assertFalse(BoundingBox(0,0,50,50).overlaps(BoundingBox(100,100,50,50)))
    def test_bbox_translate(self):
        bb = BoundingBox(10,20,100,50).translate(5,-10)
        self.assertAlmostEqual(bb.x, 15)
    def test_text_run(self):
        r = TextRun(id='r1', text='Hello', font='Times', font_size=12)
        self.assertEqual(r.vtype, VisualNodeType.TEXT_RUN)
        self.assertEqual(r.text, 'Hello')
    def test_line(self):
        line = Line(id='l1')
        line.add_run(TextRun(id='r1', text='Test'))
        self.assertEqual(len(line.runs), 1)
        self.assertEqual(line.text, 'Test')
    def test_paragraph(self):
        p = Paragraph(id='p1', bbox=BoundingBox(50,50,400,60))
        p.add_line(Line(id='l1'))
        self.assertEqual(len(p.lines), 1)
    def test_page(self):
        p = Page(id='p0', width=612, height=792, page_num=0)
        self.assertEqual(p.vtype, VisualNodeType.PAGE)
        self.assertAlmostEqual(p.bbox.width, 612)
    def test_image(self):
        img = Image(id='img1', image_path='/img.png', dpi=150)
        self.assertEqual(img.vtype, VisualNodeType.IMAGE)
    def test_formula(self):
        f = Formula(id='f1', latex='E=mc^2')
        self.assertEqual(f.vtype, VisualNodeType.FORMULA)
    def test_tree_basic(self):
        t = VisualTree()
        t.add_page(Page(id='p0', width=612, height=792, page_num=0))
        self.assertEqual(t.page_count, 1)
        self.assertIsNotNone(t.get_page(0))
    def test_tree_walk(self):
        t = VisualTree()
        p = Page(id='p0', width=612, height=792, page_num=0)
        para = Paragraph(id='p1')
        line = Line(id='l1')
        line.add_run(TextRun(id='r1', text='Walk test'))
        para.add_line(line)
        p.add_paragraph(para)
        t.add_page(p)
        self.assertGreater(len(list(t.walk())), 3)
    def test_tree_find(self):
        t = VisualTree()
        p = Page(id='p0', width=612, height=792, page_num=0)
        para = Paragraph(id='p1')
        p.add_child(para)
        t.add_page(p)
        self.assertIsNotNone(t.find('p1'))
    def test_tree_to_text(self):
        t = VisualTree()
        p = Page(id='p0', width=612, height=792, page_num=0)
        para = Paragraph(id='p1')
        line = Line(id='l1')
        line.add_run(TextRun(id='r1', text='Hello'))
        para.add_line(line)
        p.add_paragraph(para)
        t.add_page(p)
        self.assertIn('Hello', t.to_text())
    def test_glyph_run(self):
        g = GlyphRun(id='g1')
        self.assertEqual(g.vtype, VisualNodeType.GLYPH_RUN)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestEvaluator(unittest.TestCase):
    def test_clamp(self):
        self.assertAlmostEqual(clamp(150), 100)
        self.assertAlmostEqual(clamp(-10), 0)
        self.assertAlmostEqual(clamp(75.5), 75.5)
    def test_result_dict(self):
        r = EvaluationResult(translation_score=85, semantic_score=90, typography_score=95, layout_score=80, consistency_score=88)
        self.assertIn('total_score', r.to_dict())
    def test_translation_eval(self):
        o, t = DocumentGraph(), DocumentGraph()
        o.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,100,20), text='Original', page_num=0))
        t.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,100,20), text='Translated', page_num=0))
        self.assertAlmostEqual(TranslationEvaluator.evaluate(o,t)[0], 100)
    def test_semantic_eval(self):
        o, t = DocumentGraph(), DocumentGraph()
        o.add_node(DocumentNode('h1', NodeType.HEADING, (0,0,500,25), text='Heading', page_num=0))
        o.add_node(DocumentNode('p1', NodeType.PARAGRAPH, (0,25,500,100), text='Para', page_num=0))
        t.add_node(DocumentNode('h1', NodeType.HEADING, (0,0,500,25), text='Heading_tr', page_num=0))
        t.add_node(DocumentNode('p1', NodeType.PARAGRAPH, (0,25,500,100), text='Para_tr', page_num=0))
        t.add_edge(Edge('h1','p1',EdgeType.CAPTION_OF))
        self.assertAlmostEqual(SemanticEvaluator.evaluate(o,t)[0], 100)
    def test_typography_eval(self):
        g = DocumentGraph()
        for i in range(10):
            g.add_node(DocumentNode(f'n{i}', NodeType.PARAGRAPH, (0,i*20,500,i*20+15), text=f'L{i}', page_num=0, font_size=12))
        self.assertGreaterEqual(TypographyEvaluator.evaluate(g)[0], 90)
    def test_layout_no_overlap(self):
        g = DocumentGraph()
        g.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,100,20), page_num=0))
        g.add_node(DocumentNode('b', NodeType.PARAGRAPH, (0,25,100,45), page_num=0))
        self.assertEqual(LayoutEvaluator.evaluate(g)[0], 100)
    def test_layout_overlap(self):
        g = DocumentGraph()
        g.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,100,50), page_num=0))
        g.add_node(DocumentNode('b', NodeType.PARAGRAPH, (0,25,100,75), page_num=0))
        sc, det = LayoutEvaluator.evaluate(g)
        self.assertLess(sc, 100)
        self.assertIn('overlap_count', det)
    def test_consistency(self):
        g = DocumentGraph()
        g.add_node(DocumentNode('a', NodeType.PARAGRAPH, (0,0,100,20), text='foo translation', page_num=0))
        sc, _ = ConsistencyEvaluator.evaluate(g, {'foo': 'bar'})
        self.assertLess(sc, 100)
    def test_consistency_nogloss(self):
        sc, _ = ConsistencyEvaluator.evaluate(DocumentGraph())
        self.assertEqual(sc, 100)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestScheduler(unittest.TestCase):
    def test_task(self):
        t = Task('t1', 'Test', module='test', priority=10)
        self.assertEqual(t.status, TaskStatus.PENDING)
    def test_dependency(self):
        b = Task('b', 'B')
        b.depends_on('a')
        self.assertIn('a', b.dependencies)
    def test_status_props(self):
        t = Task('t1', 'Test')
        self.assertTrue(t.is_ready)
        t.status = TaskStatus.DONE
        self.assertTrue(t.is_terminal)
        self.assertFalse(t.is_ready)
    def test_can_retry(self):
        t = Task('t1', 'Test', max_retries=2)
        self.assertTrue(t.can_retry)
        t.retry_count = 2
        self.assertFalse(t.can_retry)
    def test_graph_add(self):
        tg = TaskGraph()
        tg.add_task(Task('a', 'A'))
        self.assertIsNotNone(tg.get_task('a'))
    def test_graph_duplicate(self):
        tg = TaskGraph()
        tg.add_task(Task('a','A'))
        with self.assertRaises(ValueError):
            tg.add_task(Task('a','Again'))
    def test_ready_tasks(self):
        tg = TaskGraph()
        tg.add_task(Task('a','A'))
        tg.add_task(Task('b','B', dependencies={'a'}))
        self.assertEqual(len(tg.get_ready_tasks()), 1)
    def test_topological_sort(self):
        tg = TaskGraph()
        a = Task('a','A',priority=20); b = Task('b','B',priority=10); c = Task('c','C',priority=5)
        c.depends_on('a'); c.depends_on('b')
        tg.add_task(a); tg.add_task(b); tg.add_task(c)
        order = tg.topological_sort()
        self.assertLess(next(i for i,t in enumerate(order) if t.id=='a'), next(i for i,t in enumerate(order) if t.id=='c'))
    def test_executor(self):
        tg = TaskGraph(); r = {}
        tg.add_task(Task('a','A', handler=lambda t: r.__setitem__('a','done')))
        Executor(tg).run_all()
        self.assertEqual(r.get('a'), 'done')
    def test_executor_exception(self):
        tg = TaskGraph()
        def fail(t): raise RuntimeError('fail')
        tg.add_task(Task('a','A', handler=fail, max_retries=1))
        Executor(tg).run_all()
        self.assertEqual(tg.get_task('a').status, TaskStatus.FAILED)
    def test_executor_selective(self):
        tg = TaskGraph(); r = {}
        def h(t): r[t.id] = 'done'
        a = Task('a','A',handler=h); b = Task('b','B',handler=h)
        b.depends_on('a'); tg.add_task(a); tg.add_task(b)
        Executor(tg).run_selective({'b'})
        self.assertIn('a', r)
    def test_scheduler(self):
        s = Scheduler(); r = {}
        s.create_task('a','A', handler=lambda t: r.__setitem__('a','done'))
        s.run(); self.assertIn('a', r)
    def test_stats(self):
        s = Scheduler()
        s.create_task('a','A')
        self.assertEqual(s.get_stats()['total'], 1)
    def test_remove_dep(self):
        tg = TaskGraph()
        tg.add_task(Task('a','A'))
        tg.add_task(Task('b','B', dependencies={'a'}))
        tg.remove_task('a')
        self.assertNotIn('a', tg.get_task('b').dependencies)
    def test_is_complete(self):
        tg = TaskGraph()
        tg.add_task(Task('a','A'))
        Executor(tg).run_all()
        self.assertTrue(tg.is_complete)



@unittest.skipIf(not _HAS_V3, 'V3 not importable')
class TestServiceRegistry(unittest.TestCase):
    def setUp(self):
        ServiceRegistry.reset_instance()
        self.reg = ServiceRegistry.get_instance()
    def test_register_get(self):
        self.reg.register(ParserService, 'mock')
        self.assertEqual(self.reg.get(ParserService), 'mock')
    def test_register_duplicate(self):
        self.reg.register(ParserService, 'p1')
        with self.assertRaises(ValueError):
            self.reg.register(ParserService, 'p2')
    def test_replace_flag(self):
        self.reg.register(ParserService, 'old')
        self.reg.register(ParserService, 'new', replace=True)
        self.assertEqual(self.reg.get(ParserService), 'new')
    def test_get_or_default(self):
        self.assertIsNone(self.reg.get_or_default(ParserService))
        self.assertEqual(self.reg.get_or_default(ParserService, 'd'), 'd')
    def test_replace_method(self):
        self.reg.register(ParserService, 'old')
        self.reg.replace(ParserService, 'new')
        self.assertEqual(self.reg.get(ParserService), 'new')
    def test_unregister(self):
        self.reg.register(ParserService, 'p')
        self.reg.unregister(ParserService)
        self.assertFalse(self.reg.has(ParserService))
    def test_has(self):
        self.assertFalse(self.reg.has(ParserService))
        self.reg.register(ParserService, 'p')
        self.assertTrue(self.reg.has(ParserService))
    def test_list(self):
        self.reg.register(ParserService, 'p')
        self.reg.register(AnalyzerService, 'a')
        names = self.reg.list_services()
        self.assertIn('ParserService', names)
        self.assertIn('AnalyzerService', names)
    def test_clear(self):
        self.reg.register(ParserService, 'p')
        self.reg.clear()
        self.assertFalse(self.reg.has(ParserService))
    def test_singleton(self):
        self.assertIs(ServiceRegistry.get_instance(), self.reg)
    def test_factory(self):
        called = []
        self.reg.register_factory(ParserService, lambda: (called.append(1), 'f')[1])
        self.assertEqual(self.reg.get(ParserService), 'f')
    def test_interfaces(self):
        self.assertTrue(issubclass(ParserService, ServiceInterface))
        self.assertTrue(issubclass(MemoryService, ServiceInterface))
    def test_reset(self):
        self.reg.register(ParserService, 'p1')
        ServiceRegistry.reset_instance()
        self.assertFalse(ServiceRegistry.get_instance().has(ParserService))



if __name__ == '__main__':
    unittest.main(verbosity=2)
