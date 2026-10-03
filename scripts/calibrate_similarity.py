#!/usr/bin/env python3
"""Calibrate inject_min_similarity for the active embedding model.

Embeds a labeled set of (query, chunk) pairs with the configured embedder,
measures raw cosine-similarity distributions for:
  - POSITIVE   : query and chunk are the same topic (should inject)
  - HARD NEG   : same domain, different topic (should NOT inject; closest call)
  - EASY NEG   : different domain (should NEVER inject)
and prints distribution stats + a precision/recall sweep to pick a floor.

The metric matches Mneme exactly: the query and chunk vectors are L2-normalized
and compared by dot product (== cosine), which is what FAISS IndexFlatIP scores
and what the `inject_min_similarity` floor filters on.
"""
import os
import sys
import json
import time
import statistics
import numpy as np
import requests

EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen/qwen3-embedding-8b")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))
OR_BASE = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
OR_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# ---------------------------------------------------------------------------
# Labeled dataset: {domain: [{q: query, c: chunk}, ...]}
# 8 domains x 6 topics = 48 topics. Queries are short (like a user turn),
# chunks are factual memory-like paragraphs.
# ---------------------------------------------------------------------------
DATASET = {
 "programming": [
  {"q": "how do python list comprehensions work",
   "c": "Python list comprehensions are a concise syntax for building lists by iterating over an iterable and applying an optional condition. The form [expr for item in iterable if cond] evaluates expr for each item that passes cond, producing a new list in a single expression. They are typically faster than an equivalent for-loop because the loop runs in optimized C-level bytecode, and they are idiomatic for simple transformations and filters. Readability suffers when the expression or condition grows complex, at which point a regular loop is preferred."},
  {"q": "what is a database index and why does it help",
   "c": "A database index is a data structure, usually a B-tree, that stores a sorted subset of a table's columns plus pointers to the full rows. It lets the query planner locate matching rows in logarithmic time instead of scanning every row. Indexes speed up lookups, range scans, and ORDER BY / JOIN operations, but they cost extra storage and slow down INSERT, UPDATE and DELETE because the index must be maintained on every write. Choosing columns based on query patterns, not blindly indexing everything, is the standard guidance."},
  {"q": "difference between TCP and UDP",
   "c": "TCP and UDP are transport-layer protocols. TCP is connection-oriented: it establishes a session, guarantees ordered and reliable delivery through acknowledgements and retransmission, and uses flow and congestion control, at the cost of latency. UDP is connectionless and fire-and-forget: it sends datagrams with no delivery guarantee, no ordering, and no retransmission, which makes it faster and lower-overhead, ideal for live audio, video, gaming and DNS queries where occasional loss is preferable to delay."},
  {"q": "what does git rebase do",
   "c": "Git rebase rewrites a branch's commit history by replaying its commits on top of another branch's tip, producing a linear history. In contrast to a merge, which creates a merge commit preserving both timelines, rebase moves the entire feature branch so it appears to have been developed from the latest base. It yields a cleaner, easier-to-read history but rewrites commit hashes, so it is unsafe on shared or published branches unless coordinated with collaborators, and it may require resolving conflicts commit by commit."},
  {"q": "explain the idea of a REST API",
   "c": "REST, or Representational State Transfer, is an architectural style for network APIs centered on resources identified by URLs and manipulated through standard HTTP methods: GET to read, POST to create, PUT or PATCH to update, and DELETE to remove. Each request is stateless, carrying all the information the server needs. Responses are representations, commonly JSON, of the resource. REST leverages HTTP status codes and cacheability, and its conventions make APIs predictable and easy to consume across languages and clients."},
  {"q": "what is a closure in javascript",
   "c": "A JavaScript closure is a function that retains access to variables from its enclosing lexical scope even after that scope has finished executing. Because functions in JavaScript carry a reference to the environment in which they were created, an inner function returned from an outer function keeps the outer function's variables alive. Closures enable data encapsulation, partial application, and callback state, but they can also hold large objects in memory unintentionally if references are not released."},
 ],
 "science": [
  {"q": "how does photosynthesis produce oxygen",
   "c": "Photosynthesis is the process by which plants, algae and some bacteria convert light energy into chemical energy. In the light-dependent reactions, chlorophyll absorbs photons and uses the energy to split water molecules, releasing oxygen gas as a byproduct and generating ATP and NADPH. The light-independent Calvin cycle then uses that energy to fix carbon dioxide into glucose. Oxygen released by photosynthesis is the primary source of the oxygen in Earth's atmosphere."},
  {"q": "what is the difference between mass and weight",
   "c": "Mass is an intrinsic property of matter that measures its resistance to acceleration and the amount of substance it contains; it is constant regardless of location. Weight is the gravitational force exerted on that mass, equal to mass multiplied by the local acceleration due to gravity. An object has the same mass on Earth and on the Moon, but its weight is about one-sixth on the Moon because the Moon's gravitational field is weaker."},
  {"q": "explain the structure of an atom",
   "c": "An atom consists of a dense central nucleus containing positively charged protons and electrically neutral neutrons, surrounded by a cloud of negatively charged electrons. The number of protons defines the element and is called the atomic number. Electrons occupy discrete energy levels or orbitals around the nucleus. Most of an atom's volume is empty space, while nearly all of its mass is concentrated in the nucleus."},
  {"q": "what causes ocean tides on earth",
   "c": "Ocean tides are caused primarily by the gravitational pull of the Moon and, to a lesser extent, the Sun on Earth's oceans. The Moon's gravity pulls water toward the side of Earth facing it, creating a bulge, while a second bulge forms on the opposite side due to inertia and the weaker pull there. As Earth rotates through these bulges, coastlines experience roughly two high and two low tides each day. The Sun's alignment with the Moon produces higher spring tides and lower neap tides."},
  {"q": "what is the greenhouse effect",
   "c": "The greenhouse effect is the trapping of heat in a planet's atmosphere by gases such as carbon dioxide, methane and water vapor. These gases allow incoming visible sunlight to reach the surface but absorb and re-radiate outgoing infrared radiation, warming the lower atmosphere. The effect is natural and essential for keeping Earth habitable, but human emissions have increased greenhouse gas concentrations, intensifying the effect and driving global warming."},
  {"q": "how do antibiotics kill bacteria",
   "c": "Antibiotics kill or inhibit bacteria through several mechanisms. Some, like penicillins, disrupt cell wall synthesis, causing the bacterium to burst. Others interfere with protein synthesis by binding to ribosomes, or block DNA replication, or inhibit essential metabolic pathways. Because antibiotics target structures specific to bacteria, they do not affect human cells directly, but they are ineffective against viruses, which is why antibiotics should not be used for viral infections."},
 ],
 "history": [
  {"q": "what started world war one",
   "c": "World War One began in 1914 and was triggered by the assassination of Archduke Franz Ferdinand of Austria-Hungary in Sarajevo. The assassination set off a cascade of alliances and ultimatums that drew the great powers into war within weeks. Deeper causes included militarism, imperial rivalries, nationalism and a rigid system of alliances. The conflict eventually involved dozens of nations and fundamentally reshaped the political map of Europe."},
  {"q": "what was the significance of the printing press",
   "c": "The printing press, developed by Johannes Gutenberg in the mid-fifteenth century, used movable metal type to mass-produce books quickly and cheaply. It dramatically reduced the cost of written material, spreading literacy, standardizing languages, and accelerating the dissemination of scientific and religious ideas. The press is widely credited with fueling the Renaissance, the Reformation, and the Scientific Revolution by making knowledge broadly accessible for the first time."},
  {"q": "describe the causes of the american revolution",
   "c": "The American Revolution grew out of escalating disputes between the thirteen colonies and Britain over taxation and governance. Colonists objected to taxes such as the Stamp Act and the Tea Act, imposed without their representation in Parliament, summarized in the slogan no taxation without representation. Grievances over trade restrictions, quartering of troops, and loss of self-government led to open conflict at Lexington and Concord in 1775 and the Declaration of Independence in 1776."},
  {"q": "what was the industrial revolution",
   "c": "The Industrial Revolution was a period from the late eighteenth through the nineteenth century when manufacturing shifted from hand production to machines and factories. It began in Britain, driven by innovations like the steam engine, mechanized textile production, and iron smelting. It transformed economies from agrarian to industrial, spurred urbanization, and produced enormous gains in productivity, while also creating harsh working conditions and widening social inequality that later fueled reform movements."},
  {"q": "why did the roman empire fall",
   "c": "The decline of the Roman Empire resulted from a combination of internal and external pressures. Economically it suffered from inflation, heavy taxation and reliance on slave labor. Politically it faced corruption, civil wars and weak leadership. Militarily it was pressured by invasions from Germanic tribes and other groups, and the empire was split into western and eastern halves. The western empire is conventionally dated as falling in 476 CE when the last emperor was deposed."},
  {"q": "what was the cold war about",
   "c": "The Cold War was the geopolitical rivalry between the United States and the Soviet Union from the late 1940s until the collapse of the Soviet Union in 1991. It was fought through proxy wars, nuclear arms races, espionage and ideological competition between capitalism and communism rather than direct large-scale conflict between the two superpowers. Key flashpoints included the Berlin blockade, the Cuban Missile Crisis, and the wars in Korea and Vietnam."},
 ],
 "finance": [
  {"q": "what is compound interest",
   "c": "Compound interest is interest calculated on both the initial principal and the accumulated interest of previous periods. Because each period's interest is added to the balance, growth accelerates over time, an effect often described as earning interest on interest. The formula A equals P times one plus r over n raised to the n t power captures it, where r is the annual rate, n the compounding frequency, and t the time in years. Compounding frequency and time are the dominant drivers of long-term growth."},
  {"q": "explain what a stock dividend is",
   "c": "A dividend is a portion of a company's earnings distributed to its shareholders, usually paid in cash on a regular schedule such as quarterly. Companies with stable profits often pay dividends to attract income-focused investors. The dividend yield is the annual dividend divided by the share price. Dividends are declared by the board of directors and are not guaranteed; a company may cut or suspend them during financial difficulty."},
  {"q": "what does diversification mean in investing",
   "c": "Diversification is the practice of spreading investments across different assets, sectors, geographies and asset classes to reduce risk. Because different investments do not move in perfect lockstep, losses in one area can be offset by gains or stability in another. Diversification reduces unsystematic risk specific to a single company or sector, though it cannot eliminate systematic market risk. It is a foundational principle of portfolio construction."},
  {"q": "what is inflation and how is it measured",
   "c": "Inflation is the rate at which the general level of prices for goods and services rises, eroding the purchasing power of money. It is commonly measured by indexes such as the Consumer Price Index, which tracks the price of a representative basket of goods and services over time. Central banks typically target a low, stable inflation rate around two percent, adjusting interest rates to keep inflation near that target. High inflation hurts savers and fixed-income earners most."},
  {"q": "how do bonds work",
   "c": "A bond is a debt instrument in which an investor lends money to an issuer, typically a government or corporation, in exchange for periodic interest payments and the return of the principal at maturity. The bond's face value, coupon rate and maturity date define its cash flows. Bond prices move inversely to market interest rates: when prevailing rates rise, existing bonds with lower coupons fall in value. Bonds are generally considered lower risk than stocks but offer lower returns."},
  {"q": "what is a credit score used for",
   "c": "A credit score is a numerical representation of a borrower's creditworthiness, derived from their history of borrowing and repayment. Lenders use it to decide whether to extend credit and at what interest rate. Scores are based on factors including payment history, amounts owed, length of credit history, new credit and types of credit. A higher score indicates lower risk and typically qualifies a borrower for better loan terms and lower rates."},
 ],
 "health": [
  {"q": "what are the benefits of aerobic exercise",
   "c": "Aerobic exercise, such as running, cycling or swimming, strengthens the heart and lungs by elevating the heart rate over a sustained period. It improves cardiovascular endurance, lowers resting heart rate and blood pressure, helps regulate blood sugar, and supports weight management. Regular aerobic activity also improves mood through the release of endorphins and reduces the risk of heart disease, stroke and type two diabetes. Guidelines typically recommend at least 150 minutes of moderate aerobic activity per week."},
  {"q": "how does the immune system fight infection",
   "c": "The immune system defends the body through an innate and an adaptive response. The innate response is fast and generic, using barriers like skin and cells such as macrophages that engulf pathogens. The adaptive response is slower but specific, producing antibodies and memory cells that target a particular pathogen. Vaccination works by exposing the immune system to a harmless form of a pathogen so it builds memory, enabling a faster response on later exposure."},
  {"q": "what is the difference between type 1 and type 2 diabetes",
   "c": "Type 1 diabetes is an autoimmune condition in which the immune system destroys the insulin-producing beta cells of the pancreas, requiring lifelong insulin therapy. It typically appears in childhood or early adulthood. Type 2 diabetes is characterized by insulin resistance, where the body's cells respond poorly to insulin, and is strongly associated with obesity, inactivity and genetics. Type 2 diabetes is often manageable with lifestyle changes and oral medications, though insulin may eventually be needed."},
  {"q": "why is sleep important for health",
   "c": "Sleep is essential for physical and mental restoration. During sleep the body repairs tissue, consolidates memory, regulates hormones and clears metabolic waste from the brain. Chronic sleep deprivation impairs attention, mood and immune function and raises the risk of obesity, diabetes, cardiovascular disease and depression. Most adults need seven to nine hours per night, and both sleep quantity and quality matter for long-term health."},
  {"q": "what does a balanced diet consist of",
   "c": "A balanced diet provides the nutrients the body needs in appropriate proportions. It emphasizes a variety of fruits, vegetables, whole grains, lean proteins and healthy fats while limiting added sugars, saturated fats and sodium. It supplies macronutrients for energy and micronutrients such as vitamins and minerals for physiological function. Balance, variety and moderation are the guiding principles, and hydration through adequate water intake is part of a healthy eating pattern."},
  {"q": "how do vaccines provide immunity",
   "c": "Vaccines train the immune system to recognize and fight a pathogen without causing the disease. They introduce a weakened, inactivated, or fragmentary part of a pathogen, or the genetic instructions for one of its proteins. The immune system mounts a response and produces memory cells that remain after the vaccine is cleared. On later exposure to the real pathogen, these memory cells enable a rapid, effective response, preventing or reducing the severity of illness."},
 ],
 "food": [
  {"q": "what is the maillard reaction in cooking",
   "c": "The Maillard reaction is a chemical reaction between amino acids and reducing sugars that occurs when food is heated, typically above about 140 degrees Celsius. It produces hundreds of flavor and aroma compounds and the characteristic browning of seared meat, toasted bread, roasted coffee and baked goods. It is distinct from caramelization, which involves only sugars. Managing heat and moisture is key to maximizing browning without burning."},
  {"q": "how do you temper chocolate",
   "c": "Tempering chocolate is the process of heating and cooling it to specific temperatures so the cocoa butter crystallizes into a stable form. This gives chocolate a glossy finish, a firm snap and resistance to melting at room temperature. The process typically involves melting, cooling to around 27 degrees Celsius while stirring, then gently reheating to a working temperature. Properly tempered chocolate is used for molded candies and coatings."},
  {"q": "what is the difference between baking soda and baking powder",
   "c": "Baking soda is pure sodium bicarbonate, a base that produces carbon dioxide gas when it reacts with an acid and moisture, leavening baked goods. Baking powder is a complete leavener containing sodium bicarbonate plus an acid, often cream of tartar, so it reacts without a separate acidic ingredient. Baking soda requires an acid in the recipe, while baking powder works on its own, and some recipes use both to balance rise and flavor."},
  {"q": "explain the role of gluten in bread",
   "c": "Gluten is a network of proteins, primarily glutenin and gliadin, that forms when wheat flour is mixed with water and kneaded. This elastic network traps carbon dioxide produced by yeast fermentation, allowing dough to rise and giving bread its structure and chewy texture. Kneading develops the gluten strands, and resting lets them relax. Flours vary in protein content, with higher-protein bread flour producing stronger gluten and chewier loaves."},
  {"q": "how do you safely cook chicken",
   "c": "Chicken must be cooked to a safe internal temperature to kill harmful bacteria such as Salmonella and Campylobacter. The recommended minimum internal temperature for poultry is 74 degrees Celsius, or 165 degrees Fahrenheit, measured at the thickest part without touching bone. Cross-contamination must be avoided by using separate cutting boards and utensils for raw chicken and washing hands and surfaces thoroughly. Resting the meat after cooking helps distribute juices."},
  {"q": "what is fermentation in food",
   "c": "Fermentation is a metabolic process in which microorganisms such as yeast and bacteria convert sugars into acids, gases or alcohol. It is used to make bread, beer, wine, yogurt, cheese, sauerkraut and kimchi. Fermentation preserves food by lowering pH and producing antimicrobial compounds, and it develops distinctive flavors and textures. Lactic acid, ethanol and carbon dioxide are among the common end products depending on the organism."},
 ],
 "travel": [
  {"q": "what is the best time to visit japan",
   "c": "Japan is most popularly visited in spring, from late March to early May, for the cherry blossom season, and in autumn, from October to November, for colorful fall foliage. Both seasons offer mild weather, though they are also peak tourist periods with higher prices and crowds. Summer is hot and humid with a rainy season, while winter offers skiing and hot springs, especially in the north and mountainous regions."},
  {"q": "what should i know about visiting machu picchu",
   "c": "Machu Picchu is an ancient Incan citadel in the Andes of Peru, reached via the town of Aguas Calientes by train or on foot. It sits at high altitude, around 2400 meters, so visitors should acclimatize to avoid altitude sickness. Entry is ticketed with timed slots and daily visitor limits, so booking in advance is essential. The dry season from May to September offers the clearest weather, while the wet season brings lush greenery and fewer crowds."},
  {"q": "how does jet lag work and how to reduce it",
   "c": "Jet lag occurs when rapid travel across time zones desynchronizes the body's internal circadian clock from the local day-night cycle. Symptoms include fatigue, insomnia and digestive upset. It is generally more severe when flying eastward, which shortens the perceived day. Reducing jet lag involves adjusting sleep schedule before travel, staying hydrated, avoiding alcohol and caffeine, and seeking morning light exposure in the destination time zone."},
  {"q": "what are the requirements for a schengen visa",
   "c": "A Schengen visa allows short stays of up to 90 days within any 180-day period across most European Union countries and several neighboring states. Applicants typically need a valid passport, travel insurance, proof of accommodation, proof of financial means, a flight itinerary and a completed application form, plus biometrics. The visa must be applied for at the consulate of the main destination country, and processing usually takes a few weeks."},
  {"q": "what currency is used in thailand",
   "c": "Thailand uses the Thai baht as its official currency, subdivided into satang. The baht is issued by the Bank of Thailand and is one of the most traded currencies in Southeast Asia. Cash is still widely used for small purchases and markets, though cards and mobile payments are increasingly accepted in cities and tourist areas. Exchange rates fluctuate, so travelers often compare rates between banks and licensed exchange booths."},
  {"q": "what is the great barrier reef",
   "c": "The Great Barrier Reef is the world's largest coral reef system, stretching over 2,300 kilometers along the northeastern coast of Australia. It comprises thousands of individual reefs and islands and is home to a vast diversity of marine life, including hundreds of coral species and fish species. It is a UNESCO World Heritage site and a major tourism destination, but it faces serious threats from climate change, coral bleaching and ocean acidification."},
 ],
 "technology": [
  {"q": "what is a large language model",
   "c": "A large language model is an artificial intelligence system trained on vast amounts of text to predict and generate language. Built on neural networks, typically transformer architectures with billions of parameters, they learn statistical patterns in language that let them answer questions, summarize, translate and write code. They are trained in stages, often including self-supervised pretraining followed by instruction tuning and reinforcement learning from human feedback to align them with useful behavior."},
  {"q": "how does a transformer neural network work",
   "c": "The transformer is a neural network architecture built around the attention mechanism, which weighs the relevance of each token in a sequence to every other token. Unlike recurrent networks, it processes sequences in parallel, enabling efficient training on long text. It consists of encoder and decoder stacks of self-attention and feed-forward layers with residual connections and layer normalization. Transformers underpin most modern large language models and many vision and speech systems."},
  {"q": "what is the difference between supervised and unsupervised learning",
   "c": "Supervised learning trains a model on labeled examples, where each input is paired with a correct output, so the model learns to map inputs to outputs for tasks like classification and regression. Unsupervised learning works with unlabeled data, discovering structure such as clusters, patterns or latent representations without explicit correct answers. Semi-supervised and self-supervised methods fall between or derive labels from the data itself."},
  {"q": "explain what a solid state drive is",
   "c": "A solid state drive is a storage device that uses NAND flash memory to store data with no moving parts. Compared to traditional hard disk drives, SSDs offer much faster read and write speeds, lower latency, better durability against physical shock, and lower power consumption. They are more expensive per gigabyte than hard drives but have become standard in laptops and servers. Wear leveling and error correction extend their lifespan."},
  {"q": "what is the difference between cpu and gpu",
   "c": "A CPU, or central processing unit, is a general-purpose processor optimized for sequential tasks with a small number of powerful cores and complex control logic. A GPU, or graphics processing unit, contains thousands of smaller cores designed for parallel computation, originally for rendering graphics but now widely used for machine learning, scientific computing and cryptocurrency mining. GPUs excel at workloads that can be broken into many simultaneous operations."},
  {"q": "what is blockchain technology",
   "c": "Blockchain is a distributed digital ledger in which transactions are grouped into blocks and cryptographically linked into a chain. Each block contains a hash of the previous block, making the record tamper-evident, and copies are maintained across many nodes in a network. Consensus mechanisms such as proof of work or proof of stake govern how new blocks are added. Blockchain enables decentralized systems like cryptocurrencies, but its use cases raise questions about scalability and energy use."},
 ],
}

# ---------------------------------------------------------------------------
def embed_batch(texts, label):
    """Embed a list of texts via OpenRouter (single batched request), returning
    an (n, DIM) float array. Retries on transient cold-start/timeout errors."""
    if not OR_KEY:
        print("ERROR: OPENROUTER_API_KEY not set. Source /home/sean/mneme/env first.")
        sys.exit(1)
    for attempt in range(5):
        try:
            r = requests.post(
                f"{OR_BASE}/embeddings",
                headers={"Authorization": f"Bearer {OR_KEY}", "Content-Type": "application/json"},
                json={"model": EMBED_MODEL, "input": texts, "dimensions": EMBED_DIM},
                timeout=180,
            )
            if r.status_code == 200:
                data = r.json().get("data", [])
                if len(data) == len(texts):
                    arr = np.stack([np.array(d["embedding"], dtype=np.float32) for d in data])
                    norms = np.linalg.norm(arr, axis=1, keepdims=True)
                    return arr / (norms + 1e-9)  # L2-normalize == cosine convention
                print(f"  batch returned {len(data)} of {len(texts)} — retrying", flush=True)
            else:
                print(f"  status {r.status_code}: {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"  attempt {attempt+1} failed: {type(e).__name__}", flush=True)
        time.sleep(3 * (attempt + 1))
    print(f"ERROR: failed to embed ({label}) after retries")
    sys.exit(1)


def cos(a, b):
    return float(np.dot(a, b))


def stats(name, vals):
    vals = sorted(vals)
    n = len(vals)
    def pct(p):
        i = max(0, min(n - 1, int(round(p * (n - 1)))))
        return vals[i]
    return (
        f"{name:12s} n={n:4d}  min={vals[0]:.4f}  p5={pct(.05):.4f}  "
        f"median={statistics.median(vals):.4f}  mean={statistics.mean(vals):.4f}  "
        f"p95={pct(.95):.4f}  max={vals[-1]:.4f}"
    )


def main():
    domains = list(DATASET.keys())
    chunks = []      # ordered list of chunk texts
    queries = []     # ordered list of query texts
    chunk_dom = []   # domain index per chunk
    for di, dom in enumerate(domains):
        for item in DATASET[dom]:
            chunks.append(item["c"])
            queries.append(item["q"])
            chunk_dom.append(di)
    n = len(chunks)

    print(f"Embedding {n} chunks + {n} queries with {EMBED_MODEL} @ dim={EMBED_DIM} ...")
    C = embed_batch(chunks, "chunks")
    Q = embed_batch(queries, "queries")
    print(f"  embedded {n} chunks and {n} queries OK\n")

    S = Q @ C.T  # (n, n) cosine similarity matrix; S[i][j] = sim(query_i, chunk_j)

    positive, hard_neg, easy_neg = [], [], []
    for i in range(n):
        for j in range(n):
            s = S[i][j]
            if i == j:
                positive.append(s)
            elif chunk_dom[i] == chunk_dom[j]:
                hard_neg.append(s)
            else:
                easy_neg.append(s)

    print("=== Cosine-similarity distributions (query -> chunk) ===")
    print(stats("POSITIVE", positive))
    print(stats("HARD NEG", hard_neg))
    print(stats("EASY NEG", easy_neg))
    print()

    all_neg = hard_neg + easy_neg
    pos_lo = sorted(positive)[max(0, int(round(0.05 * (len(positive) - 1))))]     # 5th pct of positive
    neg_hi = sorted(all_neg)[max(0, int(round(0.95 * (len(all_neg) - 1))))]        # 95th pct of all neg
    gap = pos_lo - neg_hi
    print(f"positive p5  = {pos_lo:.4f}")
    print(f"negative p95 = {neg_hi:.4f}")
    print(f"separation gap (pos_p5 - neg_p95) = {gap:+.4f}")
    print()

    # Precision/recall sweep over candidate thresholds
    print("=== Threshold sweep (precision = positives above T / all above T) ===")
    print(f"{'T':>6} {'recall+':>8} {'fpr-':>8} {'F1':>7}")
    best = None
    for T in [round(x * 0.01, 2) for x in range(0, 101)]:
        tp = sum(1 for s in positive if s >= T)
        fp = sum(1 for s in all_neg if s >= T)
        recall = tp / len(positive)
        fpr = fp / len(all_neg)
        prec = tp / (tp + fp) if (tp + fp) else 1.0
        f1 = 2 * prec * recall / (prec + recall) if (prec + recall) else 0.0
        if best is None or f1 > best[0]:
            best = (f1, T, recall, fpr, prec)
    bf1, bT, brecall, bfpr, bprec = best
    print(f"\nBest F1 threshold = {bT:.2f}  (F1={bf1:.3f}, recall={brecall:.3f}, false-pos-rate={bfpr:.3f}, precision={bprec:.3f})")

    # Conservative recommendation: highest threshold that still keeps >=97% of positives
    # while allowing ~0% easy negatives (robust floor with a margin for drift).
    rec_T = None
    for T in [round(x * 0.01, 2) for x in range(0, 101)]:
        rec = sum(1 for s in positive if s >= T) / len(positive)
        if rec >= 0.97:
            rec_T = T
    print(f"Highest T keeping >=97% positive recall = {rec_T:.2f}")

    print("\n=== RECOMMENDATION ===")
    # A floor near the positive p5, safely above the negative p95, with margin.
    if gap > 0.02:
        rec = round((pos_lo + neg_hi) / 2, 2)
        print(f"Clean gap detected. Recommend inject_min_similarity = {rec:.2f} "
              f"(midpoint of pos-p5 {pos_lo:.2f} and neg-p95 {neg_hi:.2f}).")
    else:
        rec = rec_T if rec_T is not None else bT
        print(f"No clean gap. Recommend inject_min_similarity = {rec:.2f} "
              f"(based on recall-preserving floor / best-F1 {bT:.2f}).")
    print(f"(For reference, strategy_min_similarity should sit ~0.05-0.10 below this.)")


if __name__ == "__main__":
    main()
