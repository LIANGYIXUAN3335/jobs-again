// Deterministic resume template. Reads merged data from data.json (written next to
// this file at render time) and lays out a clean, ATS-parseable one/two-page PDF.
// The template owns ALL layout — tailoring only supplies text, never styling.
// Style: single-column, tight spacing, "Jake's Resume" school. Section order:
// Summary -> Skills -> Experience -> Education (education last, by design).

#let data = json("data.json")

#set document(title: data.name + " — Resume", author: data.name)
#set page(paper: "us-letter", margin: (x: 0.5in, top: 0.45in, bottom: 0.45in))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 10pt, fill: rgb("#1a1d21"))
// leading = space within a paragraph; spacing = space BETWEEN blocks/paragraphs.
// The old template left `spacing` at its ~1.2em default — that was the "间距太大".
#set par(justify: false, leading: 0.5em, spacing: 0.5em)

#let navy = rgb("#12314f")

// --- header ---
#align(center)[
  #text(size: 20pt, weight: "bold", fill: navy)[#data.name]
  #v(3pt, weak: true)
  #let c = data.contact
  #text(size: 9pt)[
    // Display the clean short form, but point the hyperlink at the canonical www + trailing-slash
    // URL — the bare/no-slash form gets rejected as "invalid" by some validators. Assumes
    // contact.linkedin is stored as "linkedin.com/in/<handle>" (no scheme/www/slash).
    #c.phone #h(5pt) · #h(5pt) #link("mailto:" + c.email)[#c.email] #h(5pt) · #h(5pt) #c.location #h(5pt) · #h(5pt) #link("https://www." + c.linkedin + "/")[#c.linkedin]
  ]
]
#v(3pt)

#let section(title) = {
  v(5pt)                       // breathing room between sections (≈ font-size 5)
  text(size: 11.5pt, weight: "bold", fill: navy)[#upper(title)]
  v(2pt, weak: true)
  line(length: 100%, stroke: 0.6pt + navy)
  v(3pt, weak: true)
}

#let entry(left, right, sub) = {
  grid(columns: (1fr, auto),
    text(weight: "bold")[#left], text(weight: "bold")[#right])
  if sub != none [ #text(style: "italic", size: 9.5pt)[#sub] ]
}

// --- summary (tailored; optional) ---
#if "summary" in data and data.summary != none and data.summary != "" [
  #section("Summary")
  #data.summary
]

// --- skills ---
#section("Skills")
#stack(spacing: 5.5pt, ..data.skills.map(group => [
  #text(weight: "bold")[#group.label: ] #group.items.join(", ")
]))

// --- experience ---
#section("Professional Experience")
#for job in data.experience [
  #grid(columns: (1fr, auto),
    text(weight: "bold")[#job.company], text(weight: "bold")[#job.dates])
  #v(2.5pt)                              // gap under the company line
  #grid(columns: (1fr, auto),
    text(style: "italic", size: 9.5pt)[#job.title],
    text(style: "italic", size: 9.5pt)[#job.location])
  #v(4.5pt)                              // gap under the title/location line, before bullets
  #set list(marker: text(fill: navy)[•], indent: 2pt, body-indent: 6pt, spacing: 3.5pt)
  #list(..job.bullets.map(b => [#b]))
  #v(5pt)                      // gap between experience entries (≈ font-size 5)
]

// --- education (last, by design) ---
#section("Education")
#stack(spacing: 5pt, ..data.education.map(e => entry(e.school, e.dates, e.degree)))
