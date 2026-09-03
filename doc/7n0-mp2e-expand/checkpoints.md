# 7N-0 — MP 2e Baseline (evidence-only)

- book: `tests/file/The Art of Multiprocessor Programming, 2e.pdf`
- pages: [0, 5, 8, 12, 20, 40, 80, 120, 200, 300, 400, 500, 550]
- expand probe: True
- blocks: 223

## 7N-3 classification (which path drew each block)

| class | blocks |
|---|---|
| FIX_HIT | 2 |
| PRESERVE_OR_OTHER | 64 |
| PRESERVE_REGION | 27 |
| SHIFT_DOWN | 63 |
| TOC_CHANNEL | 67 |

## Recovery decisions / steps / traces (blocks entering adaptive_layout)

- decisions: {'shrink': 22, 'clip': 70}
- steps: {'WRAP->SHRINK': 22, 'WRAP->SHRINK->CLIP': 70}
- traces: {'WRAP@11.96->SHRINK@8.64': 3, 'WRAP@11.96->SHRINK@5.0->CLIP@5.0': 5, 'WRAP@11.96->SHRINK@5.31': 1, 'WRAP@10.96->SHRINK@5.0->CLIP@5.0': 2, 'WRAP@11.0->SHRINK@5.0->CLIP@5.0': 26, 'WRAP@21.92->SHRINK@5.08': 1, 'WRAP@83.39->SHRINK@11.86->CLIP@11.86': 1, 'WRAP@9.96->SHRINK@7.2': 4, 'WRAP@8.72->SHRINK@5.0->CLIP@5.0': 4, 'WRAP@9.96->SHRINK@8.47': 6, 'WRAP@11.0->SHRINK@7.95': 2, 'WRAP@9.96->SHRINK@5.2': 1, 'WRAP@7.97->SHRINK@5.0->CLIP@5.0': 1, 'WRAP@11.0->SHRINK@6.76': 1, 'WRAP@8.72->SHRINK@6.3': 1, 'WRAP@9.96->SHRINK@5.0->CLIP@5.0': 1, 'WRAP@7.72->SHRINK@5.0->CLIP@5.0': 30, 'WRAP@7.72->SHRINK@5.58': 2}

## fixup_render_plan stats (alternate-path signals)

- preserved=42 shifted=141 overflowed=37

## render path counters

- flow_layout_used=92 flow_legacy_fallback=0 flow_overflow=70

## Per-page classification

| page | blocks | classes |
|---|---|---|
| 5 | 8 | {'PRESERVE_REGION': 5, 'SHIFT_DOWN': 3} |
| 8 | 34 | {'PRESERVE_REGION': 2, 'TOC_CHANNEL': 32} |
| 12 | 36 | {'PRESERVE_REGION': 2, 'TOC_CHANNEL': 34} |
| 20 | 6 | {'PRESERVE_REGION': 4, 'FIX_HIT': 1, 'SHIFT_DOWN': 1} |
| 40 | 18 | {'PRESERVE_OR_OTHER': 16, 'SHIFT_DOWN': 1, 'TOC_CHANNEL': 1} |
| 80 | 4 | {'PRESERVE_OR_OTHER': 2, 'SHIFT_DOWN': 2} |
| 120 | 10 | {'PRESERVE_OR_OTHER': 5, 'SHIFT_DOWN': 3, 'FIX_HIT': 1, 'PRESERVE_REGION': 1} |
| 200 | 11 | {'PRESERVE_OR_OTHER': 10, 'SHIFT_DOWN': 1} |
| 300 | 13 | {'PRESERVE_OR_OTHER': 7, 'SHIFT_DOWN': 5, 'PRESERVE_REGION': 1} |
| 400 | 15 | {'PRESERVE_OR_OTHER': 14, 'SHIFT_DOWN': 1} |
| 500 | 6 | {'PRESERVE_OR_OTHER': 2, 'PRESERVE_REGION': 1, 'SHIFT_DOWN': 3} |
| 550 | 62 | {'PRESERVE_OR_OTHER': 8, 'SHIFT_DOWN': 43, 'PRESERVE_REGION': 11} |

## Per-block checkpoints (suspicious classes first)

| block | source | translated | layout_ok | overflow | decision | steps | trace | font | class | fixup | drawn-as |
|---|---|---|---|---|---|---|---|---|---|---|---|
| p120_8 | 2 | 2（中文译文扩展探针：用于在恒等翻译之外模拟真实翻… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.97;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p12_0 | Contents | Contents（中文译文扩展探针：用于在恒等翻译… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@10.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p12_1 | xiii | xiii（中文译文扩展探针：用于在恒等翻译之外模拟… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p20_0 | CHAPTER | CHAPTER（中文译文扩展探针：用于在恒等翻译之… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p20_1 | Introduction | Introduction（中文译文扩展探针：用于在… | True | False | shrink | WRAP->SHRINK | WRAP@21.92;SHRINK@5.08 | 5.1 | PRESERVE_REGION | keep_overflow | flow |
| p20_2 | 1 | 1（中文译文扩展探针：用于在恒等翻译之外模拟真实翻… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@83.39;SHRINK@11.86;CLIP@11.86 | 11.9 | PRESERVE_REGION | keep_overflow | flow |
| p20_4 | 1 | 1（中文译文扩展探针：用于在恒等翻译之外模拟真实翻… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p300_11 | 2 | 2（中文译文扩展探针：用于在恒等翻译之外模拟真实翻… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@9.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p500_1 | Thisalgorithmresemblesour… | Thisalgorithmresemblesour… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@8.47 | 8.5 | PRESERVE_REGION | keep_overflow | flow |
| p550_15 | CAS,530 | CAS,530（中文译文扩展探针：用于在恒等翻译之… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_17 | Checkpoint,477 | Checkpoint,477（中文译文扩展探针：用… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_2 | Cachegranularity,524 | Cachegranularity,524（中文译文… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_3 | Blockingoperation,56 | Blockingoperation,56（中文译文… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_4 | Cachehit,153,523 Cachelin… | Cachehit,153,523 Cachelin… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_46 | Boundedpool, seePool | Boundedpool, seePool（中文译文… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_47 | Chickensexing,75 | Chickensexing,75（中文译文扩展探针… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_51 | C | C（中文译文扩展探针：用于在恒等翻译之外模拟真实翻… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_6 | Cachestate | Cachestate（中文译文扩展探针：用于在恒等… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_8 | exclusive,152,476,525 | exclusive,152,476,525（中文译… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p550_9 | Bottleneck | Bottleneck（中文译文扩展探针：用于在恒等… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p5_0 | Formyparents,DavidandPatr… | Formyparents,DavidandPatr… | True | False | shrink | WRAP->SHRINK | WRAP@11.96;SHRINK@8.64 | 8.6 | PRESERVE_REGION | keep_overflow | flow |
| p5_1 | –M.H. | –M.H.（中文译文扩展探针：用于在恒等翻译之外模… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p5_3 | –N.S. | –N.S.（中文译文扩展探针：用于在恒等翻译之外模… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p5_5 | –V.L. | –V.L.（中文译文扩展探针：用于在恒等翻译之外模… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p5_7 | –M.S. | –M.S.（中文译文扩展探针：用于在恒等翻译之外模… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p8_0 | Contents | Contents（中文译文扩展探针：用于在恒等翻译… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@10.96;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p8_1 | ix | ix（中文译文扩展探针：用于在恒等翻译之外模拟真实… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | PRESERVE_REGION | keep_overflow | flow |
| p120_4 | Consensusobjectinterface. | Consensusobjectinterface.… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@8.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p120_6 | • consistent:allthreadsde… | • consistent:allthreadsde… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@5.2 | 5.2 | SHIFT_DOWN | shift_down | flow |
| p120_9 | Werestrictourselvestoobje… | Werestrictourselvestoobje… | True | False | shrink | WRAP->SHRINK | WRAP@11.0;SHRINK@6.76 | 6.8 | SHIFT_DOWN | shift_down | flow |
| p200_10 | TheLockedQueueclass:aFIFO… | TheLockedQueueclass:aFIFO… | True | False | shrink | WRAP->SHRINK | WRAP@8.72;SHRINK@6.3 | 6.3 | SHIFT_DOWN | shift_down | flow |
| p20_5 | Copyright©2021ElsevierInc… | Copyright©2021ElsevierInc… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p300_10 | Countingnetworksprovideah… | Countingnetworksprovideah… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@7.2 | 7.2 | SHIFT_DOWN | shift_down | flow |
| p300_12 | thecountingnetworkswehave… | thecountingnetworkswehave… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@8.47 | 8.5 | SHIFT_DOWN | shift_down | flow |
| p300_7 | ThePeriodicnetwork. | ThePeriodicnetwork.（中文译文扩… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@8.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p300_8 | (cid:129) Ifthenumberofco… | (cid:129) Ifthenumberofco… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@7.2 | 7.2 | SHIFT_DOWN | shift_down | flow |
| p300_9 | Ifanapplicationneedsacoun… | Ifanapplicationneedsacoun… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@8.47 | 8.5 | SHIFT_DOWN | shift_down | flow |
| p400_12 | TheWorkStealingThreadclas… | TheWorkStealingThreadclas… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@8.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p40_7 | Usingalockobject. | Usingalockobject.（中文译文扩展探… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@8.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p500_2 | set,toensurethatthevaluei… | set,toensurethatthevaluei… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@8.47 | 8.5 | SHIFT_DOWN | shift_down | flow |
| p500_3 | atransaction’sprogressiso… | atransaction’sprogressiso… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@7.2 | 7.2 | SHIFT_DOWN | shift_down | flow |
| p500_5 | systems,whichusehardwaret… | systems,whichusehardwaret… | True | False | shrink | WRAP->SHRINK | WRAP@11.0;SHRINK@7.95 | 8.0 | SHIFT_DOWN | shift_down | flow |
| p550_1 | Blockinglockimplementatio… | Blockinglockimplementatio… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_10 | Bouncer,44,45 | Bouncer,44,45（中文译文扩展探针：用于… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_11 | Call,method, seeMethodcall | Call,method, seeMethodcal… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_12 | Boundedqueue, seeQueue | Boundedqueue, seeQueue（中文… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_13 | Capacity(ofhashtable), se… | Capacity(ofhashtable), se… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_14 | Boundedtimestamp, seeTime… | Boundedtimestamp, seeTime… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_16 | Boundedwait-freeproperty,… | Boundedwait-freeproperty,… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_18 | Boundedworkstealingdeque,… | Boundedworkstealingdeque,… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_19 | Bounded-rangepriorityqueu… | Bounded-rangepriorityqueu… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_20 | Class,52 universal,129 Cl… | Class,52 universal,129 Cl… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_21 | Bucket(ofhashtable), seeH… | Bucket(ofhashtable), seeH… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_22 | Closedaddressing, seeHash… | Closedaddressing, seeHash… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_23 | Buffer,229 | Buffer,229（中文译文扩展探针：用于在恒等… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_24 | Cluster(inNUMAsystem),166 | Cluster(inNUMAsystem),166… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_25 | write, seeWritebuffer | write, seeWritebuffer（中文译… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_26 | Clusterlock,168 | Clusterlock,168（中文译文扩展探针：… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_27 | Bus,152,475,522 | Bus,152,475,522（中文译文扩展探针：… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_28 | Clusteringalgorithm,406 C… | Clusteringalgorithm,406 C… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_30 | Cohort,168 | Cohort,168（中文译文扩展探针：用于在恒等… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_32 | Collectoperation,92 Colli… | Collectoperation,92 Colli… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_33 | std::lock_guard,510 | std::lock_guard,510（中文译文扩… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_35 | dirty,528 | dirty,528（中文译文扩展探针：用于在恒等翻… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_37 | replacementpolicy,524 | replacementpolicy,524（中文译… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_38 | Completemethodcall, seeMe… | Completemethodcall, seeMe… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_39 | Cachecoherence,transactio… | Cachecoherence,transactio… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_40 | problems,471 | problems,471（中文译文扩展探针：用于在… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_41 | Cachecoherenceprotocol,15… | Cachecoherenceprotocol,15… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_42 | Computability,1 | Computability,1（中文译文扩展探针：… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_43 | MESI,476,524,525 | MESI,476,524,525（中文译文扩展探针… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_44 | modiﬁed,476,525 | modiﬁed,476,525（中文译文扩展探针：… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_45 | Cache-coherentNUMA(cc-NUM… | Cache-coherentNUMA(cc-NUM… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_48 | Coarse-grainedsynchroniza… | Coarse-grainedsynchroniza… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_49 | Busy-waiting, seeSpinning | Busy-waiting, seeSpinning… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_5 | Blockingprogresscondition… | Blockingprogresscondition… | True | False | shrink | WRAP->SHRINK | WRAP@7.72;SHRINK@5.58 | 5.6 | SHIFT_DOWN | shift_down | flow |
| p550_52 | Cohortdetection,locksuppo… | Cohortdetection,locksuppo… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_53 | Combining,software, seeSo… | Combining,software, seeSo… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_54 | std::recursive_mutex,509 | std::recursive_mutex,509（… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_55 | Combiningtreebarrier, see… | Combiningtreebarrier, see… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_56 | Common2register, seeRegis… | Common2register, seeRegis… | True | False | shrink | WRAP->SHRINK | WRAP@7.72;SHRINK@5.58 | 5.6 | SHIFT_DOWN | shift_down | flow |
| p550_59 | direct-mapped,477,524 | direct-mapped,477,524（中文译… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_60 | Comparisonnetwork,293 iso… | Comparisonnetwork,293 iso… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_61 | fullyassociative,524 | fullyassociative,524（中文译文… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@11.0;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p550_7 | Booleanregister, seeRegis… | Booleanregister, seeRegis… | False | True | clip | WRAP->SHRINK->CLIP | WRAP@7.72;SHRINK@5.0;CLIP@5.0 | 5.0 | SHIFT_DOWN | shift_down | flow |
| p5_2 | ForNounandAliza,Shaﬁ,Yona… | ForNounandAliza,Shaﬁ,Yona… | True | False | shrink | WRAP->SHRINK | WRAP@11.96;SHRINK@5.31 | 5.3 | SHIFT_DOWN | shift_down | flow |
| p5_4 | Formyfamily,especiallymyp… | Formyfamily,especiallymyp… | True | False | shrink | WRAP->SHRINK | WRAP@11.96;SHRINK@8.64 | 8.6 | SHIFT_DOWN | shift_down | flow |
| p5_6 | ForEmily,Theodore,Bernade… | ForEmily,Theodore,Bernade… | True | False | shrink | WRAP->SHRINK | WRAP@11.96;SHRINK@8.64 | 8.6 | SHIFT_DOWN | shift_down | flow |
| p80_1 | Thenonblockingpropertydoe… | Thenonblockingpropertydoe… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@8.47 | 8.5 | SHIFT_DOWN | shift_down | flow |
| p80_3 | 3.8 Progressconditions Th… | 3.8 Progressconditions Th… | True | False | shrink | WRAP->SHRINK | WRAP@11.0;SHRINK@7.95 | 8.0 | SHIFT_DOWN | shift_down | flow |
| p120_7 | Inotherwords,aconcurrentc… | Inotherwords,aconcurrentc… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@8.47 | 8.5 | FIX_HIT | keep | flow |
| p20_3 | Atthedawnofthetwenty-ﬁrst… | Atthedawnofthetwenty-ﬁrst… | True | False | shrink | WRAP->SHRINK | WRAP@9.96;SHRINK@7.2 | 7.2 | FIX_HIT | keep | flow |
| p120_0 | 104 CHAPTER5 Therelativep… | 104 CHAPTER5 Therelativep… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p120_1 | 1 public interface Consen… | 1 public interface Consen… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p120_2 | 3 } | 3 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p120_3 | FIGURE5.1 | FIGURE5.1（中文译文扩展探针：用于在恒等翻… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p120_5 | 5.1 Consensusnumbers Cons… | 5.1 Consensusnumbers Cons… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p12_10 | 19.8 Chapternotes........… | 19.8 Chapternotes........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_11 | CHAPTER20Transactionalpro… | CHAPTER20Transactionalpro… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_12 | 20.1 Challengesinconcurre… | 20.1 Challengesinconcurre… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_13 | 20.1.1Problemswithlocking… | 20.1.1Problemswithlocking… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_14 | 20.1.2Problemswithexplici… | 20.1.2Problemswithexplici… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_15 | 20.1.3Problemswithnonbloc… | 20.1.3Problemswithnonbloc… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_16 | 20.1.4Problemswithcomposi… | 20.1.4Problemswithcomposi… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_17 | 20.1.5Summary ...........… | 20.1.5Summary ...........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_18 | 20.2 Transactionalprogram… | 20.2 Transactionalprogram… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_19 | 20.2.1Anexampleoftransact… | 20.2.1Anexampleoftransact… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_2 | 18.6 Terminationdetection… | 18.6 Terminationdetection… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_20 | 20.3 Hardwaresupportfortr… | 20.3 Hardwaresupportfortr… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_21 | 20.3.1Hardwarespeculation… | 20.3.1Hardwarespeculation… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_22 | 20.3.2Basiccachecoherence… | 20.3.2Basiccachecoherence… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_23 | 20.3.3Transactionalcachec… | 20.3.3Transactionalcachec… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_24 | 20.3.4Limitationsofhardwa… | 20.3.4Limitationsofhardwa… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_25 | 20.4.1Discussion ........… | 20.4.1Discussion ........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_26 | 20.5 Transactionalmemory … | 20.5 Transactionalmemory … | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_27 | 20.5.1Run-timescheduling.… | 20.5.1Run-timescheduling.… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_28 | 20.5.2Explicitself-abort … | 20.5.2Explicitself-abort … | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_29 | 20.6 Softwaretransactions… | 20.6 Softwaretransactions… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_3 | 18.7 Chapternotes........… | 18.7 Chapternotes........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_30 | 20.6.1Transactionswithown… | 20.6.1Transactionswithown… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_31 | 20.6.2Transactionswithval… | 20.6.2Transactionswithval… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_32 | 20.7 Combininghardwareand… | 20.7 Combininghardwareand… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_33 | 20.9 Chapternotes........… | 20.9 Chapternotes........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_34 | 20.10 Exercises..........… | 20.10 Exercises..........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_35 | APPENDIXA Softwarebasics … | APPENDIXA Softwarebasics … | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_4 | CHAPTER19Optimismandmanua… | CHAPTER19Optimismandmanua… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_5 | 19.1 TransitioningfromJav… | 19.1 TransitioningfromJav… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_6 | 19.2 Optimismandexplicitr… | 19.2 Optimismandexplicitr… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_7 | 19.4 Anobjectformanagingm… | 19.4 Anobjectformanagingm… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_8 | 19.6 Hazardpointers .....… | 19.6 Hazardpointers .....… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p12_9 | 19.7 Epoch-basedreclamati… | 19.7 Epoch-basedreclamati… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p200_0 | 188 CHAPTER8 Monitorsandb… | 188 CHAPTER8 Monitorsandb… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p200_1 | 3 final Condition notFull… | 3 final Condition notFull… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_2 | items = (T[])new Object[c… | items = (T[])new Object[c… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_3 | try { while (count == ite… | try { while (count == ite… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_4 | } | } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_5 | 24 public T deq() { | 24 public T deq() { | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_6 | try { | try { | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_7 | } | } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_8 | 39 } | 39 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p200_9 | FIGURE8.5 | FIGURE8.5（中文译文扩展探针：用于在恒等翻… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p300_0 | 288 CHAPTER12 Counting,so… | 288 CHAPTER12 Counting,so… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p300_1 | 2 Block[] block; | 2 Block[] block;（中文译文扩展探针… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p300_2 | for (int i = 0; i < logSi… | for (int i = 0; i < logSi… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p300_3 | block[i] = new Block(widt… | block[i] = new Block(widt… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p300_4 | 14 } | 14 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p300_5 | 21 } 22 } | 21 } 22 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p300_6 | FIGURE12.22 | FIGURE12.22（中文译文扩展探针：用于在恒… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p400_0 | 390 CHAPTER16 Schedulinga… | 390 CHAPTER16 Schedulinga… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p400_1 | 2 DEQue[] queue; 3 public… | 2 DEQue[] queue; 3 public… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_10 | } | } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_11 | 22 } 23 } | 22 } 23 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_13 | 16.4.2 Yieldingandmultipr… | 16.4.2 Yieldingandmultipr… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p400_14 | 16.5 Work-stealingdeques … | 16.5 Work-stealingdeques … | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p400_2 | 5 } | 5 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_3 | 6 public void run() { int… | 6 public void run() { int… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_4 | RecursiveAction task = qu… | RecursiveAction task = qu… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_5 | while (true) { while (tas… | while (true) { while (tas… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_6 | task = queue[me].popBotto… | task = queue[me].popBotto… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_7 | while (task == null) { | while (task == null) { | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_8 | int victim = ThreadLocalR… | int victim = ThreadLocalR… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p400_9 | } | } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_0 | 2.2 Criticalsections 23 | 2.2 Criticalsections 23（中… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p40_1 | // to protect critical se… | // to protect critical se… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_10 | 3. thethreadcallstheunloc… | 3. thethreadcallstheunloc… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p40_11 | PRAGMA2.2.1 | PRAGMA2.2.1（中文译文扩展探针：用于在恒… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p40_12 | InJava,thelock()andunlock… | InJava,thelock()andunlock… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_13 | // body | // body | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_14 | 3 ... | 3 ... | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_15 | 5 ... // restore invarian… | 5 ... // restore invarian… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_16 | 7 } | 7 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_17 | Thisidiomensuresthatthelo… | Thisidiomensuresthatthelo… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p40_2 | // enter critical section | // enter critical section | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_3 | // in critical section //… | // in critical section //… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_4 | return temp; | return temp; | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_5 | // leave critical section | // leave critical section | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_6 | lock.unlock(); } 14 } 15 } | lock.unlock(); } 14 } 15 } | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p40_8 | Fig.2.3showshowtouseaLock… | Fig.2.3showshowtouseaLock… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p40_9 | 1. eachcriticalsectionisa… | 1. eachcriticalsectionisa… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p500_0 | 492 CHAPTER20 Transaction… | 492 CHAPTER20 Transaction… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p500_4 | 20.7 Combininghardwareand… | 20.7 Combininghardwareand… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p550_0 | 542 Index | 542 Index（中文译文扩展探针：用于在恒等翻… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p550_29 | Busy(forlock), seeLock | Busy(forlock), seeLock | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p550_31 | C++ | C++ | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p550_34 | CompareAndSet(),116,119,1… | CompareAndSet(),116,119,1… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p550_36 | complete(H),61 | complete(H),61 | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p550_50 | CoarseList,206,207 Cohere… | CoarseList,206,207 Cohere… | None | None | - | - | - | - | PRESERVE_OR_OTHER | shift_down | wrapped |
| p550_57 | C++memorymodel,512 Cache,… | C++memorymodel,512 Cache,… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p550_58 | Compare-and-swap(CAS),116… | Compare-and-swap(CAS),116… | None | None | - | - | - | - | PRESERVE_OR_OTHER | preserve | wrapped |
| p80_0 | 64 CHAPTER3 Concurrentobj… | 64 CHAPTER3 Concurrentobj… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep_overflow | wrapped |
| p80_2 | 3.7 Memoryconsistencymode… | 3.7 Memoryconsistencymode… | None | None | - | - | - | - | PRESERVE_OR_OTHER | keep | wrapped |
| p8_10 | 6.5 Chapternotes.........… | 6.5 Chapternotes.........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_11 | PART2 Practice CHAPTER7 S… | PART2 Practice CHAPTER7 S… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_12 | 7.1 Welcometotherealworld… | 7.1 Welcometotherealworld… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_13 | 7.2 Volatileﬁeldsandatomi… | 7.2 Volatileﬁeldsandatomi… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_14 | 7.5 Queuelocks...........… | 7.5 Queuelocks...........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_15 | 7.5.1 Array-basedlocks...… | 7.5.1 Array-basedlocks...… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_16 | 7.5.2 TheCLHqueuelock....… | 7.5.2 TheCLHqueuelock....… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_17 | 7.5.3 TheMCSqueuelock....… | 7.5.3 TheMCSqueuelock....… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_18 | 7.6 Aqueuelockwithtimeout… | 7.6 Aqueuelockwithtimeout… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_19 | 7.7 Hierarchicallocks ...… | 7.7 Hierarchicallocks ...… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_2 | 5.5 Multipleassignmentobj… | 5.5 Multipleassignmentobj… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_20 | 7.7.1 Ahierarchicalback-o… | 7.7.1 Ahierarchicalback-o… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_21 | 7.7.2 Cohortlocks .......… | 7.7.2 Cohortlocks .......… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_22 | 7.7.3 Acohortlockimplemen… | 7.7.3 Acohortlockimplemen… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_23 | 7.8 Acompositelock.......… | 7.8 Acompositelock.......… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_24 | 7.9 Afastpathforthreadsru… | 7.9 Afastpathforthreadsru… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_25 | 7.10 Onelocktorulethemall… | 7.10 Onelocktorulethemall… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_26 | 7.11 Chapternotes........… | 7.11 Chapternotes........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_27 | CHAPTER8 Monitorsandblock… | CHAPTER8 Monitorsandblock… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_28 | 8.2 Monitorlocksandcondit… | 8.2 Monitorlocksandcondit… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_29 | 8.2.1 Conditions ........… | 8.2.1 Conditions ........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_3 | 5.6 Read–modify–writeoper… | 5.6 Read–modify–writeoper… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_30 | 8.2.2 Thelost-wakeupprobl… | 8.2.2 Thelost-wakeupprobl… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_31 | 8.3 Readers–writerslocks … | 8.3 Readers–writerslocks … | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_32 | 8.3.1 Simplereaders–write… | 8.3.1 Simplereaders–write… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_33 | 8.3.2 Fairreaders–writers… | 8.3.2 Fairreaders–writers… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_4 | 5.7 Common2RMWoperations … | 5.7 Common2RMWoperations … | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_5 | 5.9 Chapternotes.........… | 5.9 Chapternotes.........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_6 | CHAPTER6 Universalityofco… | CHAPTER6 Universalityofco… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_7 | 6.2 Universality.........… | 6.2 Universality.........… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_8 | 6.3 Alock-freeuniversalco… | 6.3 Alock-freeuniversalco… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
| p8_9 | 6.4 Await-freeuniversalco… | 6.4 Await-freeuniversalco… | None | None | - | - | - | - | TOC_CHANNEL | shift_down | toc |
