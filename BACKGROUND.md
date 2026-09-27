## Background / History

I had the dns-test.sh script that is now in the archive folder. I had developed this over a number of years to test periodically test dns resolvers. Sometimes due to an ISP change other times just to check and see who of the big ones was currently best. I almost always use Cloudflare's 1.1.1.1 service since I have some toher things over on Cloudflare and it is almso always the best option.

## From simnple shell script to this in 1 prompt.

Last week I was running the script and since Anthropic had released Opus 5.5 I thought well, lets see what it can do with this. On a total rambling whim I dictated this prompt, set Opus 5.5 - Ultracode effort level, and walked away. Version 1 is exactly what I got back - I don't remember how long it took because I went to dinner and forgot about it but it didn't burn more than 3-4% of my Max (5x) subscription on the day. Color me impressed. Any issues and whinging about code qualit aside, it did a damn find job coming up with something. 

### The prompt: 
Including the mis-translation of "dice them" as Dyson.

> Please rewrite it as a single page HTML JavaScript application and allow the user to select those three variables and edit the DNS provider list by adding one or removing one and also add a remove entries from the URL list to test and then add the results in a nicely formatted table and keep all of the individual results and add a way to graph them and slice them and Dyson in something interesting. Get creative and see what you can do.

From here, I'll take it apart a little bit and see if it can be improved. But there was no CLAUDE.MD or anything else in the repo. It chose Python3 and plain JavaScript / HTML all on its own, which I thought was interesting.

The thing that I'm enjoying about using AI for development tasks is that it unlocks things like this. I would have never bothered to go through all of the effort on my own to make something that looks like this and has all this functionality but with a sinlge prompt I get some very interesting results. In other projects I have had some success building some very clever things with decent quality, not slop.

