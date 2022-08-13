# Unsupervised Early Exit in DNNs with Multiple Exits

This repository contains code to perform the experiments conducted in the paper:

**Unsupervised Early Exit in DNNs with Multiple Exits**

The experimental section can be divided into two parts:

Part-1:  Train a particular multi-exit model (with a certain exit configuration- EC-1/ EC-2/ EC-3 or others) on SST-2 dataset and generate
the prediction matrix (all exit predictions, *num_samples*  *X*  *num_exits* ) for IMDb/ Yelp dataset using the trained model.

Part-2:  Evaluate UEE-UCB algorithm using the prediction matrix from Part-1.

## Dataset Access

1) All the multi-exit ElasticBERT models are trained and evaluated on SST-2 dataset available at:
[**SST-2 dataset**](http://eluebenchmark.fastnlp.top/#/task?taskId=3).

2) The final trained models are evaluated on IMDb and Yelp datasets which can be accessed from:
[**IMDb dataset**](https://web.archive.org/web/20220401065200/https://s3.amazonaws.com/fast-ai-nlp/imdb.tgz) and
[**Yelp dataset**](https://web.archive.org/web/20220401065200/https://s3.amazonaws.com/fast-ai-nlp/yelp_review_polarity_csv.tgz).


