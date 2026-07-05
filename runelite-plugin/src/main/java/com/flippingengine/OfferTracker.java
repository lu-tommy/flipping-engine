package com.flippingengine;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.time.Instant;
import javax.inject.Inject;
import javax.inject.Singleton;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.GrandExchangeOffer;
import net.runelite.api.GrandExchangeOfferState;

/**
 * Detects real fills by diffing successive offer states per slot.
 *
 * On login RuneLite replays the current state of every slot as offer-changed
 * events. We only emit a fill when we have a previous baseline for the same
 * offer (same item/price/total quantity) and its filled quantity increased —
 * so replays just (re)establish baselines and never produce phantom fills.
 *
 * These observed fills are the ground truth that replaces the backtest's
 * CAPTURE_FRACTION guess over time.
 */
@Slf4j
@Singleton
public class OfferTracker
{
	private static class SlotState
	{
		int itemId;
		int price;
		int totalQuantity;
		int quantitySold;
		long spent;
		boolean buy;
	}

	private final SlotState[] slots = new SlotState[8];
	private final EngineApiClient api;

	@Inject
	public OfferTracker(EngineApiClient api)
	{
		this.api = api;
	}

	public void reset()
	{
		for (int i = 0; i < slots.length; i++)
		{
			slots[i] = null;
		}
	}

	public void onOfferChanged(int slot, GrandExchangeOffer offer, String displayName)
	{
		if (slot < 0 || slot >= slots.length)
		{
			return;
		}
		if (offer == null || offer.getState() == GrandExchangeOfferState.EMPTY)
		{
			slots[slot] = null;
			return;
		}

		SlotState prev = slots[slot];
		boolean sameOffer = prev != null
			&& prev.itemId == offer.getItemId()
			&& prev.price == offer.getPrice()
			&& prev.totalQuantity == offer.getTotalQuantity()
			&& prev.buy == AccountStateBuilder.isBuy(offer.getState());

		if (sameOffer && offer.getQuantitySold() > prev.quantitySold)
		{
			int qty = offer.getQuantitySold() - prev.quantitySold;
			long spentDelta = offer.getSpent() - prev.spent;
			emitFill(offer, qty, spentDelta, displayName);
		}

		SlotState next = new SlotState();
		next.itemId = offer.getItemId();
		next.price = offer.getPrice();
		next.totalQuantity = offer.getTotalQuantity();
		next.quantitySold = offer.getQuantitySold();
		next.spent = offer.getSpent();
		next.buy = AccountStateBuilder.isBuy(offer.getState());
		slots[slot] = next;
	}

	private void emitFill(GrandExchangeOffer offer, int qty, long spentDelta, String displayName)
	{
		JsonObject fill = new JsonObject();
		fill.addProperty("ts", Instant.now().getEpochSecond());
		fill.addProperty("item_id", offer.getItemId());
		fill.addProperty("type", AccountStateBuilder.isBuy(offer.getState()) ? "buy" : "sell");
		fill.addProperty("offer_price", offer.getPrice());
		fill.addProperty("quantity", qty);
		// actual gp moved for this delta; per-unit price can differ from
		// offer_price because the GE fills at the best available price
		fill.addProperty("spent", spentDelta);
		fill.addProperty("display_name", displayName == null ? "" : displayName);

		JsonArray fills = new JsonArray();
		fills.add(fill);
		JsonObject body = new JsonObject();
		body.add("fills", fills);
		log.debug("observed fill: {}", fill);
		api.sendFills(body);
	}
}
