// Intrusive, allocation-free slot waiting order. Caller holds one mutex for
// all queue/slot access, including ticket destruction; the queue outlives
// tickets.
#pragma once

#include <cstddef>
#include <span>

#include "q4t/runtime/sequence_capacity.h"

namespace q4t::server {

class SequenceAdmissionQueue {
 public:
  class Ticket {
   public:
    explicit Ticket(size_t required, size_t client = 0)
        : required_(required), client_(client) {}
    ~Ticket();
    Ticket(const Ticket&) = delete;
    Ticket& operator=(const Ticket&) = delete;
    bool Queued() const { return owner_ != nullptr; }

   private:
    friend class SequenceAdmissionQueue;
    const size_t required_;
    const size_t client_;
    SequenceAdmissionQueue* owner_ = nullptr;
    Ticket* previous_ = nullptr;
    Ticket* next_ = nullptr;
  };

  SequenceAdmissionQueue() = default;
  SequenceAdmissionQueue(const SequenceAdmissionQueue&) = delete;
  SequenceAdmissionQueue& operator=(const SequenceAdmissionQueue&) = delete;
  size_t Size() const { return size_; }
  // Set once before serving, with a validated immutable client inventory.
  void ConfigureClients(size_t count) {
    clients_ = count;
    next_client_ = 0;
  }
  void Granted(const Ticket& ticket) noexcept {
    if (clients_) next_client_ = (ticket.client_ + 1) % clients_;
  }

  bool Enqueue(Ticket& ticket, size_t limit) noexcept {
    if (ticket.Queued() || size_ >= limit) return false;
    if (clients_) {
      if (ticket.client_ >= clients_) return false;
      size_t waiting = 0;
      for (auto* entry = head_; entry; entry = entry->next_)
        waiting += entry->client_ == ticket.client_;
      if (waiting >= limit / clients_) return false;
    }
    ticket.owner_ = this;
    ticket.previous_ = tail_;
    if (tail_)
      tail_->next_ = &ticket;
    else
      head_ = &ticket;
    tail_ = &ticket;
    ++size_;
    return true;
  }

  void Remove(Ticket& ticket) noexcept {
    if (ticket.owner_ != this) return;
    if (ticket.previous_)
      ticket.previous_->next_ = ticket.next_;
    else
      head_ = ticket.next_;
    if (ticket.next_)
      ticket.next_->previous_ = ticket.previous_;
    else
      tail_ = ticket.previous_;
    ticket.owner_ = nullptr;
    ticket.previous_ = ticket.next_ = nullptr;
    --size_;
  }

  // With clients configured, round-robin clients then oldest eligible ticket;
  // otherwise oldest eligible globally. In both cases use best-fit slots.
  // An unqueued
  // arrival may use an idle slot only when no older waiter can use any idle
  // slot. Selection does not transfer ownership or consume a queue entry.
  template <typename FreeSlots>
  int Select(const Ticket& ticket, std::span<const int> capacities, int max_len,
             const FreeSlots& free) const {
    if (ticket.owner_ && ticket.owner_ != this) return -1;
    if (clients_) {
      if (ticket.client_ >= clients_) return -1;
      const Ticket* selected = nullptr;
      size_t rank = clients_;
      int selected_slot = -1;
      const auto consider = [&](const Ticket* entry) {
        const auto order =
            (entry->client_ + clients_ - next_client_) % clients_;
        if (order >= rank) return;
        const int slot = runtime::SelectSequenceSlot(capacities, max_len, free,
                                                     entry->required_);
        if (slot >= 0) {
          selected = entry;
          selected_slot = slot;
          rank = order;
        }
      };
      for (auto* entry = head_; entry; entry = entry->next_) consider(entry);
      if (!ticket.Queued()) consider(&ticket);
      return selected == &ticket ? selected_slot : -1;
    }
    for (const Ticket* current = head_; current; current = current->next_) {
      const int slot = runtime::SelectSequenceSlot(capacities, max_len, free,
                                                   current->required_);
      if (slot >= 0) return current == &ticket ? slot : -1;
    }
    return ticket.Queued() ? -1
                           : runtime::SelectSequenceSlot(
                                 capacities, max_len, free, ticket.required_);
  }

 private:
  Ticket* head_ = nullptr;
  Ticket* tail_ = nullptr;
  size_t size_ = 0;
  size_t clients_ = 0;
  size_t next_client_ = 0;
};

inline SequenceAdmissionQueue::Ticket::~Ticket() {
  if (owner_) owner_->Remove(*this);
}

}  // namespace q4t::server
