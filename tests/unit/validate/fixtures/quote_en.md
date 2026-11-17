# Release notes: Quillstone 2.0

Quillstone 2.0 rewrites the query planner so that joins over wide tables no longer allocate intermediate buffers on the heap. In our benchmarks the median latency of the analytics workload dropped by a third, while memory use stayed flat.

- The new planner is enabled by default. Set `QUILLSTONE_PLANNER=legacy` to switch back for one more release.
- Prepared statements are now cached per connection, which removes the cost of re-planning identical queries during bursts of traffic.
- The command line tool gained a `--explain` flag that prints the chosen plan together with the estimated cost of every step.

We would like to thank everyone who reported regressions during the release candidate period. Your traces made it possible to fix the remaining slow paths before the final build.
