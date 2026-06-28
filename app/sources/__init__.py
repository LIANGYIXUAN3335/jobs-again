from . import ashby, avature, greenhouse, lever, rippling, smartrecruiters, workday

FETCHERS = {
    "greenhouse": greenhouse.fetch,
    "lever": lever.fetch,
    "ashby": ashby.fetch,
    "workday": workday.fetch,
    "smartrecruiters": smartrecruiters.fetch,
    "rippling": rippling.fetch,
    "avature": avature.fetch,
}
