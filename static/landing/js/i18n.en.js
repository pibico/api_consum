/* CONSUM-IA — landing · EN dictionary
   Keep key-for-key in lockstep with i18n.es.js. */
window.I18N_EN = {
  /* chrome */
  "ld.lang": "Language",
  "ld.enter": "Sign in",
  "ld.apiDocs": "API documentation",
  "ld.project": "CONSUM-IA project",

  /* hero */
  "ld.metaTitle": "CONSUM-IA — You know what you pay. Now you'll know why · pibiCo",
  "ld.metaDesc": "A clamp on your consumer unit and a box that listens to it: CONSUM-IA measures what each thing in your home costs and explains it in euros and plain sentences. The box keeps working even when the internet drops.",

  "ld.eyebrow": "Home energy · CONSUM-IA project",
  "ld.heroTitle": "You know what you pay. <br>Now you'll know <em>why</em>.",
  "ld.heroSub": "CONSUM-IA is a clamp on your consumer unit and a small box that listens to it. It measures what each thing uses, learns how your home actually lives, and tells you in euros and plain sentences. No jargon, and no switching supplier.",
  "ld.chip1": "No need to switch supplier",
  "ld.chip2": "Works with no internet at home",
  "ld.chip3": "Only ever sees your home",
  "ld.ctaPrimary": "See a real day",
  "ld.ctaSecondary": "Go to my installation",
  "ld.scroll": "Scroll",

  /* the day, hour by hour */
  "ld.dayKicker": "The gesture you'll make every day",
  "ld.dayTitle": "Drag across the day and see what each hour cost",
  "ld.dayLede": "This is, in miniature, what you get inside. Electricity doesn't cost the same at three in the morning as at eight in the evening, and your home doesn't use the same either. Cross those two curves and the number you actually feel appears: what you've spent so far.",
  "ld.dayHour": "Hour",
  "ld.dayPrice": "Price",
  "ld.dayUse": "Usage",
  "ld.dayRunning": "Running total",
  "ld.legendP1": "Peak (P1)",
  "ld.legendP2": "Standard (P2)",
  "ld.legendP3": "Off-peak (P3)",
  "ld.bandP1": "Peak · P1",
  "ld.bandP2": "Standard · P2",
  "ld.bandP3": "Off-peak · P3",
  "ld.dayDisclaimer": "A demonstration built on a sample day and the 2.0TD band shape. Inside, you see your own curve and the price your own contract applies.",

  /* what it knows */
  "ld.featKicker": "What it does, unadorned",
  "ld.featTitle": "Six things your home comes to know",
  "ld.featLede": "Each one carries its own small print, right there on the card. We would rather you knew where measurement ends and estimation begins.",

  "ld.f1T": "Meters for appliances that have none, on the box itself",
  "ld.f1D": "The fridge, the hob and the microwave draw power in a shape as recognisable as a signature. The box separates them out on the screen at home, without you fitting anything to those appliances.",
  "ld.f1C": "The small print: this happens on the box, not on the web. Oven, kettle and hob share a power band and can be mistaken for one another, a second appliance with a similar signature gets folded into the first, and each start is counted late. Every claim is taken from what is still unexplained, so the totals always add up.",

  "ld.f2T": "Keeps working when the line goes down",
  "ld.f2D": "Measuring, separating appliances, spotting problems and drawing your dashboard all happen inside the house, on the box itself. If the connection drops, the screen at home stays alive.",
  "ld.f2C": "The small print: to see it from outside, the box does need the line. Without it you still get euros, computed from your own tariff; what stops is the market price beyond what was already downloaded, and you cannot sign in from cold \u2014 a session already open lasts for weeks.",

  "ld.f3T": "Your bill, in plain words",
  "ld.f3D": "Upload the PDF, or a photo with image recognition switched on, and it tells you what you pay for energy, what for contracted power, what those access charges are and how much the tax takes. With your euros and your kilowatt-hours, not a worked example.",
  "ld.f3C": "The small print: the tariff it reads is shown to you to confirm before it is applied. The bill is filed if the read succeeds; if it fails, nothing is kept.",

  "ld.f4T": "A forecast that says why",
  "ld.f4D": "Not just how much you'll spend: how much is simply the household's habit, how much the cold or the heat adds, and how much the calendar does. And when there isn't enough history yet, it says so instead of inventing.",
  "ld.f4C": "The small print: if your home has no electric heating or cooling, it won't attribute weather-driven use to you or hand you climate advice.",

  "ld.f5T": "Flags anything out of the ordinary",
  "ld.f5D": "It compares each hour against what is normal in your home at that hour on that kind of day, already corrected for how cold or warm the day was. If something is left on or spending jumps, it surfaces in the app.",
  "ld.f5C": "The small print: the alert lives inside the application. None are sent by email today, and to other people there is nothing built at all.",

  "ld.f6T": "Command it from the screen at home",
  "ld.f6D": "From the box\u2019s own screen you switch your sockets on and off, and with the PRO tier you reach that same screen from outside. There is also a rule that decides when the battery should charge, by level or by price.",
  "ld.f6C": "The small print: on compatible equipment already installed. The charge rule can be viewed and edited, but the only one that exists sits in manual mode and has not acted since July: today it governs no battery at all.",

  /* our own error */
  "ld.evalKicker": "The awkward part, published",
  "ld.evalTitle": "Every week we measure where we get it wrong",
  "ld.evalLede": "Anyone can promise accuracy. Every Monday a test runs by itself comparing what the system expected against what actually happened. These are the numbers from 17 August 2026, across three pilot accounts and four main meters \u2014 one of them our own office \u2014 and about two and a half weeks of data. The price figure is not per household: it compares our forecast against the price later published. Asterisks included.",
  "ld.evalV1": "39.7<small>%</small>",
  "ld.evalV2": "no data",
  "ld.evalV3": "23.0<small>%</small>",
  "ld.evalV4": "no data",
  "ld.eval1": "between real consumption and what the anomaly detector expected for that hour, across 1,594 hour-and-meter pairs. Also floored, at 30 W",
  "ld.eval2": "for the estimated bill: 56 bills are uploaded, but none is marked closed, and closed is all the test looks at",
  "ld.eval3": "between the last price we forecast and the one later published, across 438 hours. With a 0.01 \u20ac/kWh floor in the denominator; without that floor it sits around 27.6 %",
  "ld.eval4": "for the per-appliance split: the reference clamp exists and calibration is on, but for the washer and dishwasher the estimator has not proposed a single candidate to measure",

  /* tiers */
  "ld.tierKicker": "What gets switched on, and when",
  "ld.tierTitle": "There is no wall to pay: there are things we switch on",
  "ld.tierLede": "CONSUM-IA is not on sale yet, so this is not a price list. It is what every installation carries, and what we additionally enable when a home joins the extended pilot.",
  "ld.tierBasic": "In every installation",
  "ld.tierBasicNote": "What any pilot household sees from day one.",
  "ld.tb1": "Hour-by-hour usage and cost, with its band",
  "ld.tb2": "Today's and tomorrow's price, and the best hours",
  "ld.tb3": "Approximate cost per appliance, on the sockets that already meter",
  "ld.tb4": "Anomalies, away periods and the tip of the day",
  "ld.tb5": "Sun on your roof, weather and official warnings",
  "ld.tb6": "Upload bills and store your contract",
  "ld.tierPro": "Enabled separately",
  "ld.tierProNote": "Flagged PRO in the code. We switch it on per household; it is not for sale.",
  "ld.tp1": "How the month will end, in kWh and in euros",
  "ld.tp2": "Breakdown across the P1, P2 and P3 bands",
  "ld.tp3": "Full estimate of the period's bill",
  "ld.tp4": "Export your hours to CSV",
  "ld.tp5": "Reach your box's own screen from outside",

  "ld.spotKicker": "Fifteen seconds",
  "ld.spotTitle": "The energy you never see",
  "ld.spotLede": "One lit window in a dark block, a kitchen at night, a grey box in the consumer unit and a quiet living room.",
  "ld.spotPlay": "Play",
  "ld.spotNote": "A brand piece with ambient sound. It shows no product screens and no household's data.",

  /* closing */
  "ld.ctaTitle": "We're installing in real homes",
  "ld.ctaText": "CONSUM-IA is an R&D project running in real pilots, not a product off a shelf. If you have a home and some curiosity, write to us and we'll tell you what taking part involves."
};
