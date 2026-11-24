# Introduction

The Knowledge Portal Network Claims Repository service allows users to search 
and register deposited scientific claims along with their associated provenance.

All claims in the service are modeled using the Dataset Attribution and Evidence
Profile for Evidence Retrieval (DAPPER schema). DAPPER is a LinkML schema that
models datasets, provenance, analytical processes, and claims, enabling
reproducibility of scientific analyses by humans and AI agents. 

DAPPER supports the creation of machine-readable, digital objects that possess
unique digests. Each DAPPER object contains an identifier that adheres to the
following format

```
dapper:Class_Name:digest
```

Here, `dapper` is the namespace, `Class_Name` describes the corresponding 
class name, and `digest` is a 32 character digest that is encoded using the
sha512t24 algorithm ([Hart and Prlić 2020](#hart-prlic-2020)).

An example DAPPER digest would be:
```
dapper:Claim.smaZYm2tK_iJhc9rU6rnUxvQDqP2ZYZ6
```

For more information about DAPPER, and its specific classes, please visit https://broadinstitute.github.io/dapper/model/.

## References

<a id="hart-prlic-2020"></a>
Hart, R. K., & Prlić, A. (2020). SeqRepo: A system for managing local collections of
biological sequences. *PLOS ONE*, 15(12), e0239883.
<https://doi.org/10.1371/journal.pone.0239883>


