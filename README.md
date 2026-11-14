# One in a Millenium
## Milleneum Mutuals insurance business analysis of revenue and loss.

One in a millenium is a dashboard viewer built with Executives in mind. The dashboard helps visualize the quarter's losses and revenue by simply uploading the new quarter's data (exel sheets). The previuous quarter's are uploaded and always ready for comparison to the new data.
The person in charge will be able to see where loss is happening and from where it is coming. 
Filtering will be available to monitor broker's performance as well as filtering by city, customer, product type etc.

## Data Insights

From previous data we were able to gain meaningful insight as to why profit has not been keeping up with revenue:
  The main issue found is Broker 07's performance which proved to be the source of most losses.
  Another issue was the client retention's percentage. Many clients seem to not renew their premiums and simply cancel. Reason being attributed to long wait times.
  And lastly Home insurance claims in Montreal seem to be another source of losses.

## Business Solutions:
 We suspect Broker 07 of commiting fraud insurance and to be subjected to review from senior staff. Claims have been approved too quickly as well as most of them being right under 10k the limit to pass to senior review. Most of the client retention issue also revolves on this Broker with over 1000% of cancelled renewals.
 For wait time changes we plan to have a follow with clients who have had an unusual amount of wait time before their claim being dealt with.

 Montreal homes are subject to more water damage but the premium is the same as the other cities we should consider that.

 ## Automation:

### We planned alerts for certain conditions to handle inconsistencies as soon as possible and prevent losses. Alerts will go off under following conditions:
- if a claim is handled and procedd before 15 days and the amount exceeds 4000$
- if a claim made by broker 7 is over 8k but under 10k
- if a customer has been waiting for their claim to be processed for over 45/50 days --> follows up with client to improve client retention.
- if a claim is repeated by the same customer 3 or more times
- if a claim is repeated by the same customer 2 or more times AND the same auditor is handling it
