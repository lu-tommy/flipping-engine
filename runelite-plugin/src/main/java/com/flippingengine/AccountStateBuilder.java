package com.flippingengine;

import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import javax.inject.Inject;
import javax.inject.Singleton;
import net.runelite.api.Client;
import net.runelite.api.GrandExchangeOffer;
import net.runelite.api.GrandExchangeOfferState;
import net.runelite.api.Item;
import net.runelite.api.ItemContainer;
import net.runelite.api.gameval.InventoryID;

/**
 * Translates the live client state into the JSON body for POST /api/suggestion.
 */
@Singleton
public class AccountStateBuilder
{
	static final int COINS = 995;
	static final int PLATINUM_TOKEN = 13204;
	private static final int F2P_SLOTS = 3;
	private static final int MEMBER_SLOTS = 8;

	private final Client client;
	private final FlippingEngineConfig config;

	@Inject
	public AccountStateBuilder(Client client, FlippingEngineConfig config)
	{
		this.client = client;
		this.config = config;
	}

	/** Must be called on the client thread. */
	public JsonObject build()
	{
		JsonObject state = new JsonObject();
		state.addProperty("cash", cash());
		state.addProperty("f2p_only", config.f2p());
		state.addProperty("min_profit", config.minProfit());
		state.addProperty("total_slots", config.f2p() ? F2P_SLOTS : MEMBER_SLOTS);
		state.add("offers", offers());
		state.add("inventory", heldItems());
		return state;
	}

	private long cash()
	{
		ItemContainer inv = client.getItemContainer(InventoryID.INV);
		if (inv == null)
		{
			return 0;
		}
		long gp = 0;
		for (Item item : inv.getItems())
		{
			if (item.getId() == COINS)
			{
				gp += item.getQuantity();
			}
			else if (item.getId() == PLATINUM_TOKEN)
			{
				gp += 1000L * item.getQuantity();
			}
		}
		return gp;
	}

	private JsonArray offers()
	{
		JsonArray out = new JsonArray();
		GrandExchangeOffer[] geOffers = client.getGrandExchangeOffers();
		for (int slot = 0; slot < geOffers.length; slot++)
		{
			GrandExchangeOffer o = geOffers[slot];
			if (o == null || o.getState() == GrandExchangeOfferState.EMPTY)
			{
				continue;
			}
			// completed-but-uncollected offers still occupy the slot
			JsonObject j = new JsonObject();
			j.addProperty("slot", slot);
			j.addProperty("type", isBuy(o.getState()) ? "buy" : "sell");
			j.addProperty("item_id", o.getItemId());
			j.addProperty("price", o.getPrice());
			j.addProperty("quantity", o.getTotalQuantity());
			j.addProperty("filled", o.getQuantitySold());
			out.add(j);
		}
		return out;
	}

	private JsonArray heldItems()
	{
		JsonArray out = new JsonArray();
		ItemContainer inv = client.getItemContainer(InventoryID.INV);
		if (inv == null)
		{
			return out;
		}
		for (Item item : inv.getItems())
		{
			if (item.getId() <= 0 || item.getId() == COINS || item.getId() == PLATINUM_TOKEN)
			{
				continue;
			}
			JsonObject j = new JsonObject();
			j.addProperty("item_id", item.getId());
			j.addProperty("quantity", item.getQuantity());
			out.add(j);
		}
		return out;
	}

	static boolean isBuy(GrandExchangeOfferState s)
	{
		return s == GrandExchangeOfferState.BUYING
			|| s == GrandExchangeOfferState.BOUGHT
			|| s == GrandExchangeOfferState.CANCELLED_BUY;
	}
}
