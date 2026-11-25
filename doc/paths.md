---
kernelspec:
  display_name: Python 3
  name: python3
---

```{code-cell} python
:tags: [remove-cell]
from intake_esgf import ESGFCatalog
from pprint import pprint
```

# Just the Paths

While the basic paradigm of `intake-esgf` is to return xarray datasets for everything in your catalog, we recognize that you may wish to just get the paths back and use them in creative ways.

1. You may not want to use xarray datasets. We highly recommend learning the package and using it in your research, but you may have alternatives and we do not want to prohibit you from working as you see fit.
2. The analysis script you are running may not have been written to leverage xarray datasets.
3. You may need just the paths to pass into another tool or benchmark package.
4. You may have specific options you want to pass to `xarray.open_dataset()` that our interface does not support.

There is a catalog method we call `to_path_dict()`. This works just like `to_dataset_dict()` except we do not call xarray dataset constructors on the paths returned for you. Both functions even have most of the same keyword arguments. If we perform a search

```{code-cell} python
:tags: [remove-output]
cat = ESGFCatalog().search(
    project="CMIP6",
    experiment_id="historical",
    source_id="CanESM5",
    frequency="mon",
    variable_id=["tas","gpp"],
    member_id="r1i1p1f1",
)
```

Then we can call instead the path function and then print the local paths.

```{code-cell} python
:tags: [remove-output]
paths = cat.to_path_dict()
```

```{code-cell} python
:tags: [remove-input]
pprint(paths)
```

Note that this will also check first to see if data is available locally and download if not just as with `to_dataset_dict()`. In fact, internally our `to_dataset_dict()` function calls `to_path_dict()` first. You can also use this to obtain the OPenDAP links if you prefer.


## Options

`to_path_dict()` accepts the following keywords.

- `prefer_streaming`: use streaming links (OPeNDAP, Kerchunk, CFA) instead of downloading data.
- `globus_endpoint`: the UUID of a Globus endpoint to which files will be transferred where possible.
- `globus_path`: the path relative to the endpoint root into which the files are transferred.
- `minimal_keys`: disable to build the dictionary keys from all the master id facets.
- `ignore_facets`: a facet or list of facets to leave out of the dictionary keys.
- `separator`: the string placed between facets in the dictionary keys.
- `quiet`: silence the progress bars.

## Planning without downloading

If you want to know what files a search would use, and where they can be accessed, without downloading anything, use `file_info()`. It returns one record per file and requires no authentication.

```python
infos = cat.file_info()
infos[0]["path"], infos[0]["size"], infos[0]["checksum_type"], infos[0]["checksum_hex"]
```

Each record contains the `key`, `dataset_id`, `path`, `size`, `checksum_type` (a lowercase algorithm name such as `sha256`), `checksum` (as published by the index), `checksum_hex` (the plain hex digest), `file_start`/`file_end` (from the file name), and lists of access links under `HTTPServer`, `OPENDAP`, `Globus`, `Kerchunk` and `CFA` where available. Use `intake_esgf.base.get_globus_endpoints(info)` to extract the Globus endpoint UUIDs from a record.
