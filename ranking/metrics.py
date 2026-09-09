"""Label gains follow ESCI train.py; deterministic ID tie breaking, query macro means."""
from collections import Counter
import math
import re
import unicodedata

GAINS = {'E':1.0,'S':.1,'C':.01,'I':0.0}


def tokens(text):
    normalized = ''.join(c for c in unicodedata.normalize('NFKD',text.lower()) if not unicodedata.combining(c))
    return re.findall(r'[^\W_]+',normalized,re.UNICODE)


def bm25(query, documents, *, k1=1.2, b=.75):
    """Untrained lexical baseline: term statistics within the supplied candidate pool.

    This does not pretend to be the app's full-catalog FTS5 retrieval score.
    Unicode word boundaries are weak for unsegmented Japanese; report by locale.
    """
    terms = set(tokens(query))
    counts = [Counter(tokens(doc)) for doc in documents]
    lengths = [sum(c.values()) for c in counts]
    average = sum(lengths)/max(1,len(counts)) or 1
    n = len(counts)
    document_frequency = {term:sum(term in c for c in counts) for term in terms}
    scores = []
    for count,length in zip(counts,lengths):
        score = 0.0
        for term in terms:
            tf,df = count[term],document_frequency[term]
            if tf:
                idf = math.log(1+(n-df+.5)/(df+.5))
                score += idf * tf*(k1+1)/(tf+k1*(1-b+b*length/average))
        scores.append(score)
    return scores


def metrics(labels, scores, ids):
    if not (len(labels)==len(scores)==len(ids)) or not labels or len(set(ids)) != len(ids):
        raise ValueError('Ranked candidate IDs must be nonempty, unique and aligned')
    if any(not math.isfinite(float(s)) for s in scores):
        raise ValueError('Non-finite model score')
    order = sorted(range(len(ids)),key=lambda i:(-scores[i],ids[i]))
    gains = [GAINS[label] for label in labels]
    ideal = sorted(gains,reverse=True)
    def dcg(sequence,k):
        return sum(g/math.log2(rank+2) for rank,g in enumerate(sequence[:k]))
    actual = [gains[i] for i in order]
    result = {}
    for k,name in ((10,'ndcg_at_10'),(len(ids),'ndcg_all')):
        denominator = dcg(ideal,k)
        result[name] = dcg(actual,k)/denominator if denominator else 0.0
    exact = [rank+1 for rank,i in enumerate(order) if labels[i]=='E']
    result['mrr_exact'] = 1/exact[0] if exact else 0.0
    result['hit_exact_at_1'] = float(bool(exact) and exact[0]==1)
    result['has_positive_gain'] = any(gains)
    return result
