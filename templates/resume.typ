// Deterministic resume template. Reads merged data from data.json (written next to
// this file at render time) and lays out a clean, ATS-parseable one/two-page PDF.
// The template owns ALL layout — tailoring only supplies text, never styling.

#let data = json("data.json")

#set document(title: data.name + " — Resume", author: data.name)
#set page(paper: "us-letter", margin: (x: 0.7in, y: 0.6in))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial"), size: 10pt, fill: rgb("#1a1d21"))
#set par(justify: false, leading: 0.55em)

#let navy = rgb("#12314f")

// --- header ---
#align(center)[
  #text(size: 22pt, weight: "bold", fill: navy)[#data.name]
  #v(2pt)
  #let c = data.contact
  #text(size: 9pt)[
    #c.phone #h(6pt) | #h(6pt) #c.email #h(6pt) | #h(6pt) #c.location #h(6pt) | #h(6pt) #c.linkedin
  ]
]
#v(4pt)

#let section(title) = {
  v(6pt)
  text(size: 13pt, weight: "bold", fill: navy)[#title]
  v(-4pt)
  line(length: 100%, stroke: 0.6pt + navy)
  v(2pt)
}

#let entry(left, right, sub) = {
  grid(columns: (1fr, auto),
    text(weight: "bold")[#left], text(weight: "bold")[#right])
  if sub != none [ #text(style: "italic", size: 9.5pt)[#sub] ]
  v(1pt)
}

// --- summary (tailored; optional) ---
#if "summary" in data and data.summary != none and data.summary != "" [
  #section("Summary")
  #data.summary
]

// --- education ---
#section("Education")
#for e in data.education [
  #entry(e.school, e.dates, e.degree)
  #v(2pt)
]

// --- skills ---
#section("Skills")
#for group in data.skills [
  #text(weight: "bold")[#group.label: ]
  #group.items.join(", ")
  #linebreak()
]

// --- experience ---
#section("Professional Experience & Projects")
#for job in data.experience [
  #grid(columns: (1fr, auto),
    text(weight: "bold")[#job.company], text(weight: "bold")[#job.dates])
  #grid(columns: (1fr, auto),
    text(style: "italic", size: 9.5pt)[#job.title],
    text(style: "italic", size: 9.5pt)[#job.location])
  #v(1pt)
  #for b in job.bullets [
    #grid(columns: (12pt, 1fr), align(top)[•], b)
  ]
  #v(4pt)
]
