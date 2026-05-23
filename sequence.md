
Phase	Items	Key prerequisite / open question to resolve first
0	4.1, 4.3	none
1	4.2	none — write tests for current code
2	2.5	none
3	2.3, 3.1	Decide: result objects vs direct DB writes in 2.3
4	2.4, 3.2	none — bundle as one PR
5	1.1 → 1.3 → 1.2	Decide: Progressing window + healthcheck-less handling before 1.2
6	1.4, 1.5, 3.3	Decide: does manual sync_policy suppress self-heal (before 1.4)
7	2.1, 2.2	Decide: writeback frequency + branch before 2.1
The natural first PR after Phase 0/1 is always Phase 3, since it's the highest-leverage unlock. Everything from Phase 5 onwards flows cleanly once 2.3 and 3.1 are merged.