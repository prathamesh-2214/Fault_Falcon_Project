# LogHub audit

Service map: {'cloud-api': 'OpenStack', 'compute-svc': 'Hadoop', 'storage-svc': 'HDFS', 'coord-svc': 'Zookeeper', 'node-svc': 'BGL'}

## OpenStack

- lines: 2000
- time span: 2017-05-16 00:00:00.008000+00:00 -> 2017-05-16 00:14:47.687000+00:00 (0 days 00:14:47.679000)
- levels: {'INFO': 1969, 'WARN': 31}
- distinct templates: 43
- HTTP lines with parsed latency: 1017/1017; 5xx: 0; p95 latency 0.384s
- bursts (10-line windows with >=5 non-INFO): 0
- top non-INFO templates:
  - 30 x `Unknown base file: <*>`
  - 1 x `While synchronizing instance power states, found <*> instances in the database and <*> instances on the hypervisor.`

## Hadoop

- lines: 2000
- time span: 2015-10-18 18:01:47.978000+00:00 -> 2015-10-18 18:10:55.202000+00:00 (0 days 00:09:07.224000)
- levels: {'INFO': 1040, 'WARN': 808, 'ERROR': 150, 'FATAL': 2}
- distinct templates: 114
- bursts (10-line windows with >=5 non-INFO): 1110
- top non-INFO templates:
  - 476 x `Address change detected. Old: <*>/<*>:<*> New: <*>:<*>`
  - 326 x `Failed to renew lease for [DFSClient_NONMAPREDUCE_<*>_<*>] for <*> seconds.  Will retry shortly ...`
  - 147 x `ERROR IN CONTACTING RM.`
  - 2 x `Task: attempt_<*> - exited : java.net.NoRouteToHostException: No Route to Host from  MININT-<*>/<*> to <*>:<*> failed on socket timeout exce`
  - 2 x `Task cleanup failed for attempt attempt_<*>`
  - 1 x `Container complete event for unknown container id container_<*>`
  - 1 x `Slow ReadProcessor read fields took <*>ms (threshold=<*>ms); ack: seqno: <*> status: SUCCESS status: ERROR downstreamAckTimeNanos: <*>, targ`
  - 1 x `DFSOutputStream ResponseProcessor exception  for block BP-<*>:blk_<*>`
  - 1 x `Error Recovery for block BP-<*>:blk_<*> in pipeline <*>:<*>, <*>:<*>: bad datanode <*>:<*>`
  - 1 x `DataStreamer Exception`

## HDFS

- lines: 2000
- time span: 2008-11-09 20:36:15+00:00 -> 2008-11-11 10:20:17+00:00 (1 days 13:44:02)
- levels: {'INFO': 1920, 'WARN': 80}
- distinct templates: 14
- bursts (10-line windows with >=5 non-INFO): 75
- top non-INFO templates:
  - 80 x `<*>:<*>:Got exception while serving blk_<*> to /<*>:`

## Zookeeper

- lines: 2000
- time span: 2015-07-29 17:41:44.747000+00:00 -> 2015-08-25 11:26:28.145000+00:00 (26 days 17:44:43.398000)
- levels: {'WARN': 1318, 'INFO': 669, 'ERROR': 13}
- distinct templates: 50
- bursts (10-line windows with >=5 non-INFO): 1590
- top non-INFO templates:
  - 314 x `Interrupted while waiting for message on queue`
  - 291 x `Connection broken for id <*>, my id = <*>, error =`
  - 266 x `Interrupting SendWorker`
  - 262 x `Send worker leaving thread`
  - 86 x `Cannot open channel to <*> at election address /<*>:<*>`
  - 39 x `Connection request from old client /<*>:<*>; will be dropped if server is in r-o mode`
  - 37 x `caught end of stream exception`
  - 19 x `******* GOODBYE /<*>:<*> ********`
  - 12 x `Unexpected exception causing shutdown while sock still open`
  - 3 x `Exception causing close of session <*> due to java.io.IOException: ZooKeeperServer not running`

## BGL

- lines: 2000
- time span: 2005-06-03 15:42:50.675872+00:00 -> 2006-01-03 07:13:09.127918+00:00 (213 days 15:30:18.452046)
- levels: {'INFO': 1597, 'FATAL': 347, 'ERROR': 41, 'WARN': 8, 'SEVERE': 7}
- labels: normal '-'=1857, alerts=143 {'KERNDTLB': 60, 'KERNSTOR': 30, 'APPSEV': 17, 'KERNMNTF': 11, 'KERNTERM': 7, 'KERNREC': 5, 'APPRES': 4, 'APPREAD': 3}
- distinct templates: 120
- bursts (10-line windows with >=5 non-INFO): 378
- top non-INFO templates:
  - 60 x `data TLB error interrupt`
  - 35 x `idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=<*> Function=int IdoTransportMgr::SendPacke`
  - 30 x `data storage interrupt`
  - 21 x `ciod: Error loading <*>: invalid or missing program image, No such file or directory`
  - 20 x `instruction address: <*>`
  - 19 x `ciod: Error loading /<*>: invalid or missing program image, Permission denied`
  - 18 x `ciod: LOGIN chdir(<*>) failed: No such file or directory`
  - 17 x `ciod: Error loading /<*>: invalid or missing program image, Exec format error`
  - 9 x `Lustre mount FAILED : bglio<*> : point <*>`
  - 9 x `ciod: Error reading message prefix after LOAD_MESSAGE on CioStream socket to <*>:<*>: Link has been severed`

## Thunderbird

- lines: 2000
- time span: 2005-11-09 12:01:01+00:00 -> 2005-11-09 12:15:32+00:00 (0 days 00:14:31)
- levels: {'NA': 2000}
- labels: normal '-'=2000, alerts=0 {}
- distinct templates: 149
- bursts (10-line windows with >=5 non-INFO): 0
- top non-INFO templates: none
